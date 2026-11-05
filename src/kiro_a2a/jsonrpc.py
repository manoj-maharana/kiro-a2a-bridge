"""A small JSON-RPC 2.0 peer over newline-delimited JSON.

ACP (Kiro <-> client) and MCP stdio both use this framing: one JSON object
per line, and either side may send requests. So one class covers both.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

RequestHandler = Callable[[str, Any], Awaitable[Any]]
NotificationHandler = Callable[[str, Any], None]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


class Peer:
    """One end of a JSON-RPC connection.

    `read_line` returns the next raw line (b"" at end of stream) and
    `write_line` sends one. Notifications are handled synchronously, in the
    order they arrive, so a streamed update is always seen before the
    response that ends the turn.
    """

    def __init__(
        self,
        read_line: Callable[[], Awaitable[bytes]],
        write_line: Callable[[bytes], Awaitable[None]],
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
    ):
        self._read_line = read_line
        self._write_line = write_line
        self._on_request = on_request
        self._on_notification = on_notification
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    def start(self) -> None:
        self._spawn(self._read_loop())

    async def request(self, method: str, params: Any = None) -> Any:
        if self.closed.is_set():
            raise ConnectionError("connection is closed")
        msg_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._send(_message(method, params, msg_id))
            return await future
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        if not self.closed.is_set():
            await self._send(_message(method, params))

    async def _send(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            await self._write_line(data)

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._read_line()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("ignoring non-JSON line: %r", line[:200])
                    continue
                for item in msg if isinstance(msg, list) else [msg]:
                    self._dispatch(item)
        except Exception as exc:  # broken pipe, reset, etc.
            log.debug("read loop stopped: %s", exc)
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("connection closed"))

    def _dispatch(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            if "id" in msg:
                self._spawn(self._answer(msg))
            elif self._on_notification:
                try:
                    self._on_notification(msg["method"], msg.get("params"))
                except Exception:
                    log.exception("notification handler failed for %s", msg["method"])
            return
        future = self._pending.get(msg.get("id"))
        if future is None or future.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            future.set_exception(
                RpcError(err.get("code", -32000), err.get("message", "error"), err.get("data"))
            )
        else:
            future.set_result(msg.get("result"))

    async def _answer(self, msg: dict) -> None:
        reply: dict = {"jsonrpc": "2.0", "id": msg["id"]}
        try:
            if self._on_request is None:
                raise RpcError(-32601, f"Method not found: {msg['method']}")
            reply["result"] = await self._on_request(msg["method"], msg.get("params"))
        except RpcError as exc:
            reply["error"] = exc.to_dict()
        except Exception as exc:
            log.exception("request handler failed for %s", msg["method"])
            reply["error"] = {"code": -32603, "message": str(exc) or type(exc).__name__}
        try:
            await self._send(reply)
        except Exception as exc:
            log.debug("could not send reply: %s", exc)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _message(method: str, params: Any, msg_id: int | None = None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return msg


async def run_stdio(
    on_request: RequestHandler,
    on_notification: NotificationHandler | None = None,
    on_ready: Callable[[Peer], None] | None = None,
) -> None:
    """Serve JSON-RPC on this process's stdin/stdout until stdin closes.
    stdin is read on a thread because async pipe reads on stdin do not work
    on every platform (Windows)."""
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes] = asyncio.Queue()

    def pump() -> None:
        for line in iter(sys.stdin.buffer.readline, b""):
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, b"")

    threading.Thread(target=pump, daemon=True).start()
    out = sys.stdout.buffer

    async def write_line(data: bytes) -> None:
        out.write(data)
        out.flush()

    peer = Peer(lines.get, write_line, on_request, on_notification or (lambda m, p: None))
    if on_ready:
        on_ready(peer)
    peer.start()
    await peer.closed.wait()
