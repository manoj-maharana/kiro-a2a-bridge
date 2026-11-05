import json
import os
import sys
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web

from kiro_a2a.acp import AcpAgent
from kiro_a2a.server import Bridge, make_app

FAKE_AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "kiro_a2a", "demo_agent.py")


@asynccontextmanager
async def running_bridge(permissions="read-only", api_key=None, env=None, **options):
    """Start `serve` on a free port with the fake ACP agent behind it.
    Yields (http session, base url, bridge)."""
    agent = AcpAgent([sys.executable, FAKE_AGENT], permissions=permissions, env=env)
    bridge = Bridge(agent, name="Fake Kiro", api_key=api_key, **options)
    runner = web.AppRunner(make_app(bridge))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}/"
    bridge.public_url = base
    try:
        async with aiohttp.ClientSession() as http:
            yield http, base, bridge
    finally:
        await runner.cleanup()


async def rpc(http, base, method, params, headers=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with http.post(base, json=body, headers=headers) as resp:
        return resp.status, await resp.json()


async def stream(http, base, params):
    body = {"jsonrpc": "2.0", "id": 7, "method": "message/stream", "params": params}
    events = []
    async with http.post(base, json=body) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        async for raw in resp.content:
            line = raw.decode().strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:])["result"])
    return events


def user_message(text, context_id=None):
    msg = {"kind": "message", "role": "user", "messageId": os.urandom(8).hex(), "parts": [{"kind": "text", "text": text}]}
    if context_id:
        msg["contextId"] = context_id
    return {"message": msg}
