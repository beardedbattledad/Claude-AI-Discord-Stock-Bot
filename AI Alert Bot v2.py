import os
import asyncio
import datetime
import json
from dotenv import load_dotenv
import discord
from discord.ext import commands
from anthropic import AsyncAnthropic

load_dotenv()

# CONFIG
UW_API_KEY = os.getenv("UW_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

ANTHROPIC = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ====================== TOOL DEFINITIONS ======================
TOOLS = [
    {
        "name": "get_flow_alerts",
        "description": "Get recent options flow activity. Use ticker for specific symbol (e.g. DVN) or leave blank for broad scan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Specific ticker like DVN, QQQ, SPY (optional)"},
                "since_hours": {"type": "integer", "description": "Hours to look back (default 2)"},
                "min_premium": {"type": "integer", "description": "Minimum premium in USD"},
                "limit": {"type": "integer", "default": 30}
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

# ====================== EXECUTE TOOL (Improved) ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        import httpx
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"

        if tool_name == "get_flow_alerts":
            ticker = tool_input.get("ticker")
            params = {
                "limit": min(tool_input.get("limit", 30), 50),
                "min_premium": tool_input.get("min_premium")
            }

            if ticker:
                # Ticker-specific — this is much better for questions like "DVN"
                url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            else:
                # Broad scan
                url = f"{base_url}/api/option-trades/flow-alerts"

            print(f"Calling: {url} with params: {params}")   # ← Important for logs

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                print(f"Status code: {resp.status_code}")       # ← Important for logs

                data = resp.json() if resp.status_code == 200 else {"error": resp.text}

                # Truncate safely
                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    return {
                        "count": len(data["data"]),
                        "samples": data["data"][:12],
                        "ticker": ticker or "broad",
                        "note": f"Found {len(data['data'])} items for {ticker or 'broad scan'}"
                    }
                return data

        # Other tools unchanged
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
        print(f"Tool execution error: {str(e)}")
        return {"error": str(e)}

# The rest of your code (handle_tool_loop, send_long_message, on_message, on_ready) stays exactly the same as you sent me.

# ====================== HANDLE TOOL LOOP ======================
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

# ====================== SEND LONG MESSAGES ======================
async def send_long_message(channel, text):
    if len(text) <= 1900:
        await channel.send(text)
        return

    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**Part {i}/{len(chunks)}**\n" if len(chunks) > 1 else ""
        await channel.send(prefix + chunk)

# ====================== ON MESSAGE ======================
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

# ====================== ON READY ======================
@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user} — Ready for DM tests and mentions!")

bot.run(DISCORD_TOKEN)