"""MCP stdio server that gives Kiro (IDE or CLI) access to A2A agents.

Every configured A2A agent becomes one MCP tool, `ask_<agent name>`. Kiro
calls the tool with a message; the reply text comes back together with a
context_id that Kiro can pass again to continue the same conversation.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from collections.abc import Awaitable, Callable

import aiohttp

from kiro_a2a import __version__
from kiro_a2a.a2a_client import A2AClient, A2AError
from kiro_a2a.auth import AuthError, AuthRequired
from kiro_a2a.jsonrpc import Peer, RpcError, run_stdio

Notify = Callable[[str, dict], Awaitable[None]]

log = logging.getLogger(__name__)

MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def tool_name(label: str, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:48] or "agent"
    name, n = f"ask_{slug}", 2
    while name in taken:
        name, n = f"ask_{slug}_{n}", n + 1
    return name


class McpServer:
    def __init__(self, clients: list[A2AClient], notify: Notify | None = None):
        self.clients = clients
        # sends MCP notifications back to Kiro (progress updates)
        self.notify = notify
        self.http: aiohttp.ClientSession | None = None
        self._tools: dict[str, tuple[A2AClient, dict]] = {}

    async def handle(self, method: str, params: dict | None):
        params = params or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in MCP_VERSIONS else MCP_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "kiro-a2a-bridge", "version": __version__},
                "instructions": "Each ask_* tool sends a message to a remote A2A agent. "
                "Pass the returned context_id to keep talking to the same agent conversation. "
                "If the agent asks a question (state input-required), ask the user and send "
                "the answer back with the same context_id and task_id.",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            await self._load_tools()
            return {"tools": [schema for _, schema in self._tools.values()]}
        if method == "tools/call":
            progress_token = (params.get("_meta") or {}).get("progressToken")
            return await self._call(params.get("name"), params.get("arguments") or {}, progress_token)
        raise RpcError(-32601, f"Method not found: {method}")

    async def _load_tools(self) -> None:
        """Read each agent card once. Agents that are down still get a tool,
        and their card is retried on the next tools/list."""
        if self._tools and all(client.card for client, _ in self._tools.values()):
            return
        http = self._session()
        await asyncio.gather(
            *(c.fetch_card(http) for c in self.clients if c.card is None), return_exceptions=True
        )
        tools: dict[str, tuple[A2AClient, dict]] = {}
        for client in self.clients:
            card = client.card or {}
            label = client.name or card.get("name") or re.sub(r"^https?://", "", client.url)
            name = tool_name(label, set(tools))
            tools[name] = (client, {
                "name": name,
                "description": _describe(client, card),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "What to ask or tell the agent."},
                        "context_id": {
                            "type": "string",
                            "description": "Optional. The context_id from an earlier reply, to continue that conversation.",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Only when the agent asked a question (state input-required): "
                            "the task_id from that reply, so the answer goes to the same task.",
                        },
                    },
                    "required": ["message"],
                },
            })
        self._tools = tools

    async def _call(self, name: str, args: dict, progress_token=None) -> dict:
        if not self._tools:
            await self._load_tools()
        if name not in self._tools:
            raise RpcError(-32602, f"Unknown tool: {name}")
        client, _ = self._tools[name]
        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            return _tool_result("`message` must be a non-empty string.", error=True)

        on_progress = None
        if progress_token is not None and self.notify:
            steps = itertools.count(1)

            async def on_progress(text: str, kind: str | None = None) -> None:
                await self.notify(
                    "notifications/progress",
                    {"progressToken": progress_token, "progress": next(steps), "message": text[:500]},
                )

        try:
            reply = await client.ask(
                self._session(), message, args.get("context_id") or None, args.get("task_id") or None, on_progress
            )
        except AuthError as exc:
            log.warning("sign-in problem for %s: %s", client.url, exc)
            label = client.name or name.removeprefix("ask_")
            fix = (f"Tell the user to run `kiro-a2a login {label}` in a terminal, then try again."
                   if isinstance(exc, AuthRequired)
                   else "Tell the user to fix the agent's sign-in settings; `kiro-a2a doctor` shows the problem.")
            return _tool_result(f"Could not sign in to the A2A agent at {client.url}: {exc}\n{fix}", error=True)
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, A2AError) as exc:
            log.warning("call to %s failed: %s", client.url, exc)
            unreachable = (isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError))
                           or "Could not load agent card" in str(exc))
            if unreachable:
                return _tool_result(f"Could not reach the A2A agent at {client.url}: {exc}\nThe agent did not answer. "
                                    "Tell the user to check that it is running (`kiro-a2a doctor`).", error=True)
            return _tool_result(f"The A2A agent at {client.url} returned an error: {exc}", error=True)
        except Exception as exc:
            log.exception("call to %s failed", client.url)
            return _tool_result(f"Calling the A2A agent at {client.url} failed: {exc}", error=True)

        text = reply.text or "(the agent returned no text)"
        if reply.state == "input-required":
            text = (
                f"The agent needs more information before it can continue:\n\n{text}\n\n"
                f"Ask the user, then call {name} again with their answer as `message`, "
                f"context_id={reply.context_id} and task_id={reply.task_id}."
            )
        footer = f"\n\n---\ncontext_id: {reply.context_id or '-'} | task_id: {reply.task_id or '-'} | state: {reply.state}"
        return _tool_result(text + footer, error=reply.state in ("failed", "rejected"))

    def _session(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(trust_env=True)  # honours HTTPS_PROXY / NO_PROXY
        return self.http

    async def close(self) -> None:
        if self.http and not self.http.closed:
            await self.http.close()


def _describe(client: A2AClient, card: dict) -> str:
    if not card:
        return f"A2A agent at {client.url} (its agent card could not be loaded yet)."
    lines = [card.get("description") or f"A2A agent '{card.get('name', client.url)}'."]
    for skill in card.get("skills") or []:
        lines.append(f"- {skill.get('name', skill.get('id', 'skill'))}: {skill.get('description', '')}".rstrip(": "))
    return "\n".join(lines)


def _tool_result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def serve_stdio(server: McpServer) -> None:
    def ready(peer: Peer) -> None:
        server.notify = peer.notify

    try:
        await run_stdio(server.handle, on_ready=ready)
    finally:
        await server.close()
