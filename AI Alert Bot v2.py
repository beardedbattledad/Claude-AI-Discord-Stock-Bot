import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from anthropic import AsyncAnthropic

load_dotenv()

# ====================== CONFIG ======================
UW_API_KEY = os.getenv("UW_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

ALERT_CHANNEL_ID = 1490357895710376116   # Your alert channel

# Your custom filters (change these names once you create them in Unusual Whales)
CUSTOM_FILTERS = [
    {"name": "AI ETF",      "interval_seconds": 30},
    {"name": "AI Mega Cap", "interval_seconds": 45},
    {"name": "AI Mid Cap",  "interval_seconds": 120},
    {"name": "AI Small Cap","interval_seconds": 180},
]

TEST_MODE = False

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

ANTHROPIC = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ====================== YOUR STRICT RULES (Auto-alerts ONLY) ======================
TRADING_RULES = """
Apply strictly for auto-alerts:
- Tier by market cap or ETF type.
- Major Index ETFs: ≥ $1M premium, relaxed chasing (|5%|).
- Leveraged/Inverse ETFs: ≥ $100K, flag as high-vol speculative.
- Hard filters: Aggressive sweep, new positions (vol > OI), no chasing (except ETFs), meets premium threshold.
- Prefer directional flow. Flag likely hedges.
Only alert if ALL hard filters pass with high conviction.
"""

# ====================== TOOLS (same as your stable version) ======================
TOOLS = [
    {
        "name": "get_flow_alerts",
        "description": "Get the most recent options flow activity. Default = last 200 trades (no premium or time filter unless asked).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Specific ticker like DVN (optional)"},
                "since_hours": {"type": "integer", "description": "Only use if user specifically asks for a time window"},
                "min_premium": {"type": "integer", "description": "Minimum premium — only use if user asks"},
                "limit": {"type": "integer", "default": 200}
            }
        }
    },
    {
        "name": "get_dark_pool_trades",
        "description": "Get recent dark pool prints.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 15}}
        }
    },
    {
        "name": "get_congress_trades",
        "description": "Get recent congressional trades.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}}
        }
    },
    {
        "name": "get_insider_trades",
        "description": "Get recent insider transactions.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}}
        }
    }
]

# ====================== EXECUTE TOOL (Your current stable version) ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        import httpx
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"

        if tool_name == "get_flow_alerts":
            ticker = tool_input.get("ticker")
            limit = min(tool_input.get("limit", 200), 200)
            since_hours = tool_input.get("since_hours")
            min_premium = tool_input.get("min_premium")

            params = {"limit": limit}

            if since_hours is not None:
                cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=since_hours)).isoformat()
                params["newer_than"] = cutoff

            if min_premium is not None:
                params["min_premium"] = min_premium

            if ticker:
                url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            else:
                url = f"{base_url}/api/option-trades/flow-alerts"

            print(f"→ Calling {url} | limit={limit} | since_hours={since_hours or 'None (most recent)'} | min_premium={min_premium or 'None'}")

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                print(f"→ Status: {resp.status_code}")

                data = resp.json() if resp.status_code == 200 else {"error": resp.text}

                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    results = data["data"]
                    return {
                        "count": len(results),
                        "samples": results[:150],
                        "ticker": ticker or "broad",
                        "note": f"Most recent {len(results)} trades (no default time or premium filter)"
                    }
                return data

        # Keep your other tools exactly as they were
        elif tool_name == "get_dark_pool_trades":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/darkpool/recent", headers=headers, params={"limit": tool_input.get("limit", 15)})
                data = resp.json() if resp.status_code == 200 else {"error": resp.text}
                return {"count": len(data) if isinstance(data, list) else 0, "samples": data[:6] if isinstance(data, list) else data}

        elif tool_name == "get_congress_trades":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/congress/recent-trades", headers=headers, params={"limit": tool_input.get("limit", 10)})
                return resp.json() if resp.status_code == 200 else {"error": resp.text}

        elif tool_name == "get_insider_trades":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/insider/transactions", headers=headers, params={"limit": tool_input.get("limit", 10)})
                return resp.json() if resp.status_code == 200 else {"error": resp.text}

        return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        print(f"Tool error: {str(e)}")
        return {"error": str(e)}

# ====================== SHORT ALERT FORMAT ======================
def format_short_alert(flow_item: dict) -> str:
    ticker = flow_item.get("ticker", "N/A")
    expiry = flow_item.get("expiration", "N/A")
    strike = flow_item.get("strike", "N/A")
    side = flow_item.get("side", "N/A").upper()
    premium = flow_item.get("premium", "N/A")
    vol_oi = flow_item.get("vol_oi_ratio", "N/A")
    execution = flow_item.get("execution_type", "N/A")

    return f"🚨 **{ticker}** {expiry} {strike} {side} | ${premium:,} | Vol/OI {vol_oi}x | {execution}"

# ====================== MARKET HOURS CHECK ======================
def is_market_open():
    if TEST_MODE:
        return True
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=4)
    if now.weekday() >= 5:
        return False
    return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16

# ====================== AUTO ALERT SCANNER (New - uses your rules) ======================
@tasks.loop(seconds=90)   # Slower base loop
async def auto_alert_scanner():
    if not is_market_open():
        return

    for f in CUSTOM_FILTERS:
        filter_name = f["name"]
        interval = f["interval_seconds"]

        last_run_attr = f"last_run_{filter_name.replace(' ', '_')}"
        if not hasattr(auto_alert_scanner, last_run_attr):
            setattr(auto_alert_scanner, last_run_attr, datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=interval + 10))

        last_run = getattr(auto_alert_scanner, last_run_attr)
        if (datetime.datetime.now(datetime.UTC) - last_run).total_seconds() < interval:
            continue

        try:
            tool_result = await execute_tool("get_flow_alerts", {})

            if "error" in tool_result or not tool_result.get("samples"):
                continue

            # HEAVILY TRUNCATED data for auto-alerts
            light_data = {
                "filter": filter_name,
                "count": tool_result.get("count", 0),
                "samples": tool_result.get("samples", [])[:8]   # Only 8 samples max
            }

            # Very short system prompt
            system_prompt = "Scan flow for high-conviction alerts ONLY. Apply strict rules. Return short alert if it passes all hard filters. Else return exactly: NO_ALERT"

            response = await ANTHROPIC.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=200,           # Very low
                temperature=0.0,
                messages=[{"role": "user", "content": json.dumps(light_data, default=str)}],
                system=system_prompt
            )

            reply = "".join(b.text for b in response.content if b.type == "text").strip()

            if "NO_ALERT" not in reply and reply:
                channel = bot.get_channel(ALERT_CHANNEL_ID)
                if channel:
                    await channel.send(format_short_alert(tool_result["samples"][0]))

        except Exception as e:
            if "rate_limit" in str(e).lower():
                print("Rate limit hit - sleeping 10s")
                await asyncio.sleep(10)
            else:
                print(f"Auto-alert error for {filter_name}: {e}")

        setattr(auto_alert_scanner, last_run_attr, datetime.datetime.now(datetime.UTC))

# ====================== YOUR ORIGINAL CONVERSATIONAL MODE (unchanged) ======================
async def handle_tool_loop(response, messages):
    while response.stop_reason == "tool_use":
        tool_results = []
        for content_block in response.content:
            if content_block.type == "tool_use":
                tool_name = content_block.name
                tool_input = content_block.input
                tool_use_id = content_block.id

                print(f"Claude called tool: {tool_name} with input: {tool_input}")

                result = await execute_tool(tool_name, tool_input)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(result, default=str)[:15000]
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = await ANTHROPIC.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            temperature=0.4,
            tools=TOOLS,
            messages=messages
        )

    final_text = ""
    for block in response.content:
        if block.type == "text":
            final_text += block.text
    return final_text

async def send_long_message(channel, text):
    if len(text) <= 1900:
        await channel.send(text)
        return

    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**Part {i}/{len(chunks)}**\n" if len(chunks) > 1 else ""
        await channel.send(prefix + chunk)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        query = message.clean_content.replace(f"<@{bot.user.id}>", "").strip()
        if not query:
            return

        try:
            async with message.channel.typing():
                pass
        except:
            pass

        try:
            messages = [{"role": "user", "content": query}]

            response = await ANTHROPIC.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                temperature=0.4,
                tools=TOOLS,
                messages=messages,
                system="""You are a sharp options flow and smart money trading analyst.
Use the tools to fetch real data from Unusual Whales. Be concise, evidence-based, and only highlight high-conviction setups.
Cite specific numbers (premium, dark pool prints, etc.) when possible."""
            )

            final_reply = await handle_tool_loop(response, messages)
            
            if final_reply:
                await send_long_message(message.channel, final_reply)
            else:
                await message.reply("No strong signals or data available at the moment.")

        except Exception as e:
            print(f"Error processing message: {e}")
            await message.reply("Sorry, I ran into an error while analyzing. Please try again.")

# ====================== COMMANDS ======================
@bot.command()
async def testmode(ctx, state: str = "on"):
    if ctx.author.id != 123456789012345678:   # Replace with your user ID if you want
        return
    global TEST_MODE
    TEST_MODE = state.lower() in ["on", "true", "1", "yes"]
    await ctx.send(f"Test Mode is now {'ON' if TEST_MODE else 'OFF'}")

@bot.command()
async def status(ctx):
    await ctx.send(f"Bot Online • Test Mode: {'ON' if TEST_MODE else 'OFF'} • Market Open: {is_market_open()}")

# ====================== STARTUP ======================
@bot.event
async def on_ready():
    print(f"✅ v3 Bot is online as {bot.user}")
    if not auto_alert_scanner.is_running():
        auto_alert_scanner.start()
        print("Auto-alert scanner started (using your strict rules)")

bot.run(DISCORD_TOKEN)