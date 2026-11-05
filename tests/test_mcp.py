"""`kiro-a2a mcp`: Kiro -> MCP -> A2A agent."""

import asyncio
import json
import os
import sys

from aiohttp import web
from helpers import running_bridge

from kiro_a2a.a2a_client import A2AClient
from kiro_a2a.mcp import McpServer

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


async def plain_message_agent():
    """A minimal A2A agent that answers with a Message instead of a Task."""

    async def card(request):
        return web.json_response({
            "name": "Weather Bot",
            "description": "Tells the weather.",
            "url": str(request.url.origin()) + "/rpc",
            "skills": [{"id": "w", "name": "Forecast", "description": "Daily forecast"}],
        })

    async def rpc(request):
        body = await request.json()
        text = body["params"]["message"]["parts"][0]["text"]
        return web.json_response({
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"kind": "message", "role": "agent", "messageId": "m1", "contextId": "ctx-w",
                       "parts": [{"kind": "text", "text": f"Sunny, re: {text}"}]},
        })

    app = web.Application()
    app.router.add_get("/.well-known/agent-card.json", card)
    app.router.add_post("/rpc", rpc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


def test_tools_list_and_call():
    async def run():
        weather_runner, weather_url = await plain_message_agent()
        try:
            async with running_bridge() as (_, kiro_url, _):
                server = McpServer([
                    A2AClient(kiro_url, name="kiro"),
                    A2AClient(weather_url),
                    A2AClient("http://127.0.0.1:1", name="down"),
                ])
                init = await server.handle("initialize", {"protocolVersion": "2025-06-18"})
                tools = (await server.handle("tools/list", {}))["tools"]
                first = await server.handle("tools/call", {"name": "ask_kiro", "arguments": {"message": "hi"}})
                ctx = first["content"][0]["text"].split("context_id: ")[1].split(" |")[0]
                second = await server.handle(
                    "tools/call", {"name": "ask_kiro", "arguments": {"message": "again", "context_id": ctx}}
                )
                weather = await server.handle("tools/call", {"name": "ask_weather_bot", "arguments": {"message": "today?"}})
                down = await server.handle("tools/call", {"name": "ask_down", "arguments": {"message": "x"}})
                await server.close()
        finally:
            await weather_runner.cleanup()
        return init, tools, first, second, weather, down

    init, tools, first, second, weather, down = asyncio.run(run())
    assert init["protocolVersion"] == "2025-06-18"
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"ask_kiro", "ask_weather_bot", "ask_down"}
    assert "Forecast: Daily forecast" in by_name["ask_weather_bot"]["description"]
    assert by_name["ask_kiro"]["inputSchema"]["required"] == ["message"]
    assert "Echo: hi (turn 1" in first["content"][0]["text"] and not first["isError"]
    assert "turn 2" in second["content"][0]["text"]
    assert weather["content"][0]["text"].startswith("Sunny, re: today?")
    assert "context_id: ctx-w" in weather["content"][0]["text"]
    assert down["isError"] is True


def test_stdio_end_to_end():
    """Run `python -m kiro_a2a mcp` as Kiro would, over stdin/stdout."""

    async def run():
        async with running_bridge() as (_, kiro_url, _):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "kiro_a2a", "mcp", "--agent", f"kiro={kiro_url}",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": SRC},
            )

            async def call(msg_id, method, params):
                proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode())
                await proc.stdin.drain()
                return json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))

            init = await call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}})
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            tools = await call(2, "tools/list", {})
            result = await call(3, "tools/call", {"name": "ask_kiro", "arguments": {"message": "from kiro"}})
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 10)
        return init, tools, result

    init, tools, result = asyncio.run(run())
    assert init["result"]["serverInfo"]["name"] == "kiro-a2a-bridge"
    assert [t["name"] for t in tools["result"]["tools"]] == ["ask_kiro"]
    assert "Echo: from kiro" in result["result"]["content"][0]["text"]
