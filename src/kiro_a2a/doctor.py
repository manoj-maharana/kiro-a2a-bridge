"""`kiro-a2a doctor`: check the configured agents, their sign-in, and kiro-cli."""

from __future__ import annotations

import asyncio
import os
import shlex

import aiohttp

from kiro_a2a.a2a_client import A2AClient
from kiro_a2a.acp import AcpAgent, resolve_command
from kiro_a2a.auth import AuthError, AuthRequired
from kiro_a2a.config import default_paths, read_config

OK, WARN, BAD = "ok  ", "warn", "FAIL"


def _line(status: str, text: str) -> None:
    print(f"  [{status}] {text}")


async def check_agent(http: aiohttp.ClientSession, entry: dict) -> bool:
    name = entry.get("name") or entry.get("url")
    print(f"\nagent '{name}'  {entry.get('url')}")
    try:
        client = A2AClient.from_config(entry)
    except ValueError as exc:
        _line(BAD, f"config: {exc}")
        return False
    client.auth.allow_browser = False  # a check must never start a sign-in
    try:
        card = await client.fetch_card(http)
    except Exception as exc:
        _line(BAD, f"agent card: {exc}")
        return False
    streaming = (card.get("capabilities") or {}).get("streaming")
    _line(OK, f"agent card: {card.get('name')} (streaming: {'yes' if streaming else 'no'})")
    try:
        resolved = await client.auth.resolve(http, card)
        await client.auth.headers(http, card)
        detail = resolved["type"] + (f", {resolved['flow']}" if resolved.get("flow") else "")
        _line(OK, f"sign-in: {detail}")
    except AuthRequired:
        _line(WARN, f"sign-in needed: kiro-a2a login {entry.get('name')}")
        return True
    except AuthError as exc:
        _line(BAD, f"sign-in: {exc}")
        return False
    return True


async def check_kiro(command: str) -> bool:
    print(f"\nACP agent  {command}")
    parts = shlex.split(command, posix=os.name != "nt")
    try:
        resolve_command(parts)
    except FileNotFoundError as exc:
        _line(WARN, str(exc) + "  (only needed for `serve`)")
        return True
    agent = AcpAgent(parts)
    try:
        await asyncio.wait_for(agent.ensure_started(), 60)
        info = agent.agent_info.get("agentInfo") or {}
        _line(OK, f"started: {info.get('name', 'agent')} {info.get('version', '')}".rstrip())
        methods = agent.auth_methods
        if methods:
            _line(OK, "sign-in methods (for serve --kiro-auth-method): "
                  + ", ".join(f"{m.get('id')} = {m.get('name')}" for m in methods))
        await asyncio.wait_for(agent.new_session(), 60)
        _line(OK, "signed in: a session could be opened")
        return True
    except Exception as exc:
        _line(BAD, f"{exc}")
        return False
    finally:
        await agent.close()


async def run_doctor(args) -> int:
    paths = [args.config] if args.config else [p for p in default_paths() if os.path.exists(p)]
    healthy = True
    if not paths:
        print("No agents config found. Add an agent with: kiro-a2a setup-kiro <name> <url>")
    async with aiohttp.ClientSession(trust_env=True) as http:
        for path in paths:
            print(f"config {os.path.abspath(path)}")
            try:
                agents = read_config(path).get("agents", [])
            except Exception as exc:
                _line(BAD, f"cannot read: {exc}")
                healthy = False
                continue
            for entry in agents:
                healthy = await check_agent(http, entry) and healthy
    healthy = await check_kiro(args.kiro_cmd) and healthy
    print("\nAll good." if healthy else "\nSome checks failed.")
    return 0 if healthy else 1
