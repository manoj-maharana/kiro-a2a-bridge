"""ACP client side: start `kiro-cli acp` (or any ACP agent) and talk to it.

One child process serves many sessions. If the process dies it is started
again on the next call, and `generation` goes up so callers know their old
session ids are gone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable

from kiro_a2a import __version__
from kiro_a2a.jsonrpc import Peer, RpcError

log = logging.getLogger(__name__)

ACP_PROTOCOL_VERSION = 1
POLICIES = ("read-only", "allow-all", "deny-all", "ask")
# ACP tool kinds that cannot change anything on disk or run commands.
SAFE_TOOL_KINDS = {"read", "search", "think", "fetch"}
STREAM_LIMIT = 16 * 1024 * 1024

# The ACP spec writes update kinds in snake_case; some Kiro docs show
# PascalCase (AgentMessageChunk) instead. Both map to the spec name.
UPDATE_KINDS = (
    "agent_message_chunk", "agent_thought_chunk", "user_message_chunk", "tool_call",
    "tool_call_update", "plan", "available_commands_update", "current_mode_update",
)
STOP_REASONS = ("end_turn", "cancelled", "refusal", "max_tokens", "max_turn_requests")

UpdateCallback = Callable[[dict], None]
# Called in "ask" mode: (session id, request params) -> ACP permission outcome
PermissionHandler = Callable[[str, dict], Awaitable[dict]]


def _squash(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


_UPDATE_BY_KEY = {_squash(k): k for k in UPDATE_KINDS}
_STOP_BY_KEY = {_squash(k): k for k in STOP_REASONS} | {"canceled": "cancelled", "turnend": "end_turn"}


def normalize_update(update: dict) -> dict:
    raw = update.get("sessionUpdate") or update.get("type") or ""
    name = _UPDATE_BY_KEY.get(_squash(raw))
    if name and name != update.get("sessionUpdate"):
        return {**update, "sessionUpdate": name}
    return update


def normalize_stop_reason(raw: str | None) -> str:
    return _STOP_BY_KEY.get(_squash(raw or "end_turn"), raw or "end_turn")


def pick_option(options: list[dict], allow: bool, prefer_always: bool = False) -> dict:
    """Choose a permission option of the wanted kind, or cancel if none fits."""
    if allow:
        wanted = ("allow_always", "allow_once") if prefer_always else ("allow_once", "allow_always")
    else:
        wanted = ("reject_once", "reject_always")
    for option_kind in wanted:
        for option in options:
            if option.get("kind") == option_kind:
                return {"outcome": "selected", "optionId": option["optionId"]}
    return {"outcome": "cancelled"}


def resolve_command(command: list[str]) -> list[str]:
    """Find the executable on PATH, and wrap .cmd/.bat shims on Windows."""
    if not command:
        raise ValueError("agent command is empty")
    path = shutil.which(command[0])
    if path is None:
        raise FileNotFoundError(
            f"Could not find '{command[0]}'. Install Kiro CLI and run `kiro-cli login`, "
            "or point --kiro-cmd at your ACP agent."
        )
    if sys.platform == "win32" and path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", path, *command[1:]]
    return [path, *command[1:]]


class AcpAgent:
    def __init__(
        self,
        command: list[str],
        cwd: str = ".",
        permissions: str = "read-only",
        env: dict[str, str] | None = None,
        auth_method: str | None = None,
    ):
        if permissions not in POLICIES:
            raise ValueError(f"permissions must be one of {POLICIES}")
        self.command = command
        # ACP sign-in method id (from initialize's authMethods), if the agent needs one
        self.auth_method = auth_method
        self.cwd = os.path.abspath(cwd)
        self.permissions = permissions
        self.env = env
        self.generation = 0
        self.agent_info: dict = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._peer: Peer | None = None
        self._start_lock = asyncio.Lock()
        self._listeners: dict[str, UpdateCallback] = {}
        self.permission_handler: PermissionHandler | None = None

    @property
    def alive(self) -> bool:
        return self._peer is not None and not self._peer.closed.is_set()

    async def ensure_started(self) -> Peer:
        async with self._start_lock:
            if self.alive:
                return self._peer
            await self._stop_process()
            cmd = resolve_command(self.command)
            log.info("starting ACP agent: %s", " ".join(cmd))
            env = {**os.environ, **self.env} if self.env else None
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                limit=STREAM_LIMIT,
            )

            async def write_line(data: bytes) -> None:
                proc.stdin.write(data)
                await proc.stdin.drain()

            peer = Peer(proc.stdout.readline, write_line, self._on_request, self._on_notification)
            peer.start()
            self._proc, self._peer = proc, peer
            self.generation += 1
            self._listeners.clear()
            try:
                self.agent_info = await asyncio.wait_for(
                    peer.request(
                        "initialize",
                        {
                            "protocolVersion": ACP_PROTOCOL_VERSION,
                            # We do not offer file or terminal access: Kiro uses its own tools.
                            "clientCapabilities": {
                                "fs": {"readTextFile": False, "writeTextFile": False},
                                "terminal": False,
                            },
                            "clientInfo": {"name": "kiro-a2a-bridge", "version": __version__},
                        },
                    ),
                    timeout=60,
                )
                if self.auth_method:
                    await self._authenticate(peer, self.auth_method)
            except Exception:
                await self._stop_process()
                raise
            return peer

    @property
    def auth_methods(self) -> list[dict]:
        return self.agent_info.get("authMethods") or []

    async def _authenticate(self, peer: Peer, method_id: str) -> None:
        log.info("signing in to the ACP agent with method '%s'", method_id)
        await peer.request("authenticate", {"methodId": method_id})

    async def new_session(self) -> tuple[str, int]:
        peer = await self.ensure_started()
        params = {"cwd": self.cwd, "mcpServers": []}
        try:
            result = await peer.request("session/new", params)
        except RpcError as exc:
            # ACP's auth_required error. With exactly one sign-in method, use it.
            if exc.code != -32000 or self.auth_method:
                raise
            methods = self.auth_methods
            if len(methods) != 1:
                listed = ", ".join(f"{m.get('id')} ({m.get('name')})" for m in methods) or "none offered"
                raise RpcError(exc.code, f"The agent needs a sign-in. Choose one with --kiro-auth-method: {listed}") from exc
            await self._authenticate(peer, methods[0]["id"])
            result = await peer.request("session/new", params)
        return result["sessionId"], self.generation

    async def load_session(self, session_id: str) -> bool:
        """Reopen a session from an earlier process, if the agent supports it.
        The agent replays the old conversation as updates; nobody is listening
        for this session yet, so the replay is dropped."""
        peer = await self.ensure_started()
        if not (self.agent_info.get("agentCapabilities") or {}).get("loadSession"):
            return False
        try:
            await peer.request("session/load", {"sessionId": session_id, "cwd": self.cwd, "mcpServers": []})
            return True
        except Exception as exc:
            log.warning("could not reload session %s: %s", session_id, exc)
            return False

    @property
    def prompt_capabilities(self) -> dict:
        """What the agent accepts in a prompt besides text (image, audio, embeddedContext)."""
        return (self.agent_info.get("agentCapabilities") or {}).get("promptCapabilities") or {}

    async def prompt(self, session_id: str, prompt: str | list[dict], on_update: UpdateCallback) -> dict:
        """Run one turn. Returns the ACP result, with stopReason normalized
        (end_turn, cancelled, refusal, ...)."""
        if not self.alive:
            raise ConnectionError("ACP agent is not running")
        blocks = [{"type": "text", "text": prompt}] if isinstance(prompt, str) else prompt
        self._listeners[session_id] = on_update
        try:
            result = await self._peer.request("session/prompt", {"sessionId": session_id, "prompt": blocks})
        finally:
            self._listeners.pop(session_id, None)
        result = dict(result or {})
        result["stopReason"] = normalize_stop_reason(result.get("stopReason"))
        return result

    async def cancel(self, session_id: str) -> None:
        if self.alive:
            await self._peer.notify("session/cancel", {"sessionId": session_id})

    async def close(self) -> None:
        await self._stop_process()

    def _on_notification(self, method: str, params: dict | None) -> None:
        # session/update is the spec name; session/notification appears in some Kiro docs.
        if method not in ("session/update", "session/notification") or not params:
            log.debug("ignoring ACP notification %s", method)
            return
        listener = self._listeners.get(params.get("sessionId"))
        if listener:
            update = params.get("update") or {k: v for k, v in params.items() if k != "sessionId"}
            listener(normalize_update(update))

    async def _on_request(self, method: str, params: dict | None):
        if method == "session/request_permission":
            params = params or {}
            if self.permissions == "ask" and self.permission_handler:
                return {"outcome": await self.permission_handler(params.get("sessionId"), params)}
            return {"outcome": self._decide_permission(params)}
        raise RpcError(-32601, f"Method not supported by kiro-a2a-bridge: {method}")

    def _decide_permission(self, params: dict) -> dict:
        tool = params.get("toolCall") or {}
        kind = tool.get("kind") or "other"
        allow = self.permissions == "allow-all" or (
            self.permissions == "read-only" and kind in SAFE_TOOL_KINDS
        )
        log.info("%s tool '%s' (%s)", "allowing" if allow else "denying", tool.get("title"), kind)
        listener = self._listeners.get(params.get("sessionId"))
        if listener:
            listener({"sessionUpdate": "permission", "toolCall": tool, "allowed": allow})
        return pick_option(params.get("options") or [], allow)

    async def _stop_process(self) -> None:
        proc, self._proc, self._peer = self._proc, None, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            proc.kill()
            await proc.wait()
