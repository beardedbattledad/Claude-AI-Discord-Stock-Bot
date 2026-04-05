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

ALERT_CHANNEL_ID = 1490357895710376116   # Your channel ID

CUSTOM_FILTERS = [
    {"name": "AI ETF",      "interval_seconds": 30},
    {"name": "AI Mega Cap", "interval_seconds": 45},
    {"name": "AI Mid Cap",  "interval_seconds": 120},
    {"name": "AI Small Cap","interval_seconds": 180},
]

TEST_MODE = False   # Change to True to force scanning outside market hours

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

ANTHROPIC = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ====================== STRICT RULES (Auto-alerts only) ======================
TRADING_RULES = """
Apply strictly for auto-alerts:
- Tier by market cap or ETF type.
- Major Index ETFs: ≥ $1M premium, relaxed chasing (|5%|).
- Leveraged/Inverse ETFs: ≥ $100K, flag as high-vol speculative.
- Hard filters: Aggressive sweep, new positions (vol > OI), no chasing (except ETFs), meets premium threshold.
- Prefer directional flow. Flag likely hedges.
Only alert if ALL hard filters pass with high conviction.
"""

# ====================== TOOLS ======================
TOOLS = [
    {
    "name": "get_flow_alerts",
    "description": "Get recent options flow. Can be for a specific ticker or broad scan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Specific ticker like DVN, QQQ, SPY (optional)"},
            "filter_name": {"type": "string", "description": "Custom filter name if using one"},
            "limit": {"type": "integer", "default": 20}
        }
    }
}
]

# ====================== EXECUTE TOOL ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        import httpx
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"

        if tool_name == "get_flow_alerts":
            # Support both broad scan and ticker-specific
            ticker = tool_input.get("ticker")
            if ticker:
                # Ticker-specific (best for user queries like "DVN")
                url = f"{base_url}/api/stock/{ticker}/flow-alerts"
                params = {"limit": min(tool_input.get("limit", 20), 50)}
            else:
                # Broad scan for auto-alerts or general questions
                url = f"{base_url}/api/option-trades/flow-alerts"
                params = {"limit": min(tool_input.get("limit", 30), 40)}

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                
                if resp.status_code != 200:
                    return {"error": f"API error {resp.status_code}: {resp.text}"}
                
                data = resp.json()
                
                # Truncate for safety
                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    return {
                        "filter": tool_input.get("filter_name") or ticker or "broad",
                        "count": len(data["data"]),
                        "samples": data["data"][:15],
                        "note": f"Showing up to 15 results for {ticker or 'broad scan'}."
                    }
                return data

        return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
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
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=4)  # Rough ET
    if now.weekday() >= 5:
        return False
    return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16

# ====================== AUTO ALERT SCANNER ======================
@tasks.loop(seconds=30)
async def auto_alert_scanner():
    if not is_market_open():
        return

    for f in CUSTOM_FILTERS:
        filter_name = f["name"]
        interval = f["interval_seconds"]

        last_run_attr = f"last_run_{filter_name}"
        if not hasattr(auto_alert_scanner, last_run_attr):
            setattr(auto_alert_scanner, last_run_attr, datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=interval + 10))

        last_run = getattr(auto_alert_scanner, last_run_attr)
        if (datetime.datetime.now(datetime.UTC) - last_run).total_seconds() < interval:
            continue

        try:
            tool_result = await execute_tool("get_flow_alerts", {"filter_name": filter_name})

            if "error" in tool_result or not tool_result.get("samples"):
                continue

            system_prompt = f"""You are scanning flow for alerts. Use these rules strictly:
{TRADING_RULES}

Return ONLY a short alert if it passes ALL hard filters and has high conviction.
If nothing qualifies, return exactly: NO_ALERT"""

            response = await ANTHROPIC.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                temperature=0.0,
                messages=[{"role": "user", "content": f"Filter: {filter_name}\nData: {json.dumps(tool_result)}"}],
                system=system_prompt
            )

            reply = "".join(b.text for b in response.content if b.type == "text").strip()

            if "NO_ALERT" not in reply and reply:
                channel = bot.get_channel(ALERT_CHANNEL_ID)
                if channel:
                    await channel.send(format_short_alert(tool_result["samples"][0]))

        except Exception as e:
            print(f"Auto-alert error for {filter_name}: {e}")

        setattr(auto_alert_scanner, last_run_attr, datetime.datetime.now(datetime.UTC))

# ====================== FULL CONVERSATIONAL MODE ======================
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
                max_tokens=1200,
                temperature=0.4,
                tools=TOOLS,
                messages=messages,
                system="You are a helpful and detailed options flow analyst. Answer naturally and flexibly without enforcing strict hard filters."
            )

            final_reply = "".join(b.text for b in response.content if b.type == "text")
            if final_reply:
                await message.channel.send(final_reply)
            else:
                await message.reply("No strong signals found at the moment.")

        except Exception as e:
            print(f"Conversational error: {e}")
            await message.reply("Sorry, I encountered an error processing your request.")

# ====================== COMMANDS ======================
@bot.command()
async def testmode(ctx, state: str = "on"):
    if ctx.author.id != 138517459589267456:   # Only you can use
        return
    global TEST_MODE
    TEST_MODE = state.lower() in ["on", "true", "1", "yes"]
    await ctx.send(f"✅ Test Mode is now **{'ON' if TEST_MODE else 'OFF'}**")

@bot.command()
async def status(ctx):
    if ctx.author.id != 138517459589267456:
        return
    await ctx.send(f"Bot Online • Test Mode: {'ON' if TEST_MODE else 'OFF'} • Market Open: {is_market_open()}")

# ====================== STARTUP ======================
@bot.event
async def on_ready():
    print(f"✅ v2 Bot is online as {bot.user}")
    if not auto_alert_scanner.is_running():
        auto_alert_scanner.start()
        print(f"Auto-alert scanner active → posting to channel {ALERT_CHANNEL_ID}")

bot.run(DISCORD_TOKEN)