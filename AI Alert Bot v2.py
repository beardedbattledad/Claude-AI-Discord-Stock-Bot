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
        "description": "Get recent options flow activity. Use this for broad scanning of unusual or large options trades.",
        "input_schema": {
            "type": "object",
            "properties": {
                "since_hours": {"type": "integer", "description": "Hours to look back (default 2)"},
                "min_premium": {"type": "integer", "description": "Minimum premium in USD (default 100000)"},
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

# ====================== EXECUTE TOOL (with truncation) ======================
async def execute_tool(tool_name: str, tool_input: dict):
    try:
        import httpx
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        base_url = "https://api.unusualwhales.com"

        if tool_name == "get_flow_alerts":
            ticker = tool_input.get("ticker")
            limit = min(tool_input.get("limit", 200), 200)   # Max per call is ~200
            since_hours = tool_input.get("since_hours")

            params = {"limit": limit}

            if since_hours:
                # User specifically asked for a time window
                cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=since_hours)).isoformat()
                params["newer_than"] = cutoff

            if ticker:
                url = f"{base_url}/api/stock/{ticker.upper()}/flow-alerts"
            else:
                url = f"{base_url}/api/option-trades/flow-alerts"

            print(f"→ Calling {url} | limit={limit} | since_hours={since_hours}")

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                print(f"→ Status: {resp.status_code}")

                data = resp.json() if resp.status_code == 200 else {"error": resp.text}

                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    results = data["data"]
                    return {
                        "count": len(results),
                        "samples": results[:150],   # Send up to 150 to Claude (safe limit)
                        "ticker": ticker or "broad",
                        "note": f"Most recent {len(results)} trades (max per call ~200)"
                    }
                return data

        # Keep your other tools (dark_pool, congress, insider) unchanged
        elif tool_name == "get_dark_pool_trades":
            # ... your existing code for this tool
            pass

        # ... same for congress and insider

        return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        print(f"Tool error: {str(e)}")
        return {"error": str(e)}

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
                    "content": json.dumps(result, default=str)[:15000]  # Hard cap to prevent overflow
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = await ANTHROPIC.messages.create(
            model="claude-sonnet-4-6",          # Current stable model
            max_tokens=1000,
            temperature=0.4,
            tools=TOOLS,
            messages=messages
        )

    # Extract final text
    final_text = ""
    for block in response.content:
        if block.type == "text":
            final_text += block.text
    return final_text

# ====================== SEND LONG MESSAGES (split if needed) ======================
async def send_long_message(channel, text):
    """Splits long messages into chunks of ~1900 characters"""
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

        # Safe typing (works in DMs and channels)
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