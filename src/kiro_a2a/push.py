"""A2A push notifications: POST task updates to a caller's webhook.

Off by default. Turn on with `serve --push`, and limit where updates may be
sent with `--push-host` (strongly recommended: otherwise any caller can make
the server send requests to any address it can reach).

A notification is sent when a task changes state (working, input-required,
completed, ...), not for every streamed chunk. The body is the task in the
A2A version the config was created with: the Task itself for 0.3, a
StreamResponse ({"task": ...}) for 1.0.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from urllib.parse import urlparse

import aiohttp

from kiro_a2a.jsonrpc import RpcError
from kiro_a2a.protocol import result_to_v1

log = logging.getLogger(__name__)

PUSH_NOT_SUPPORTED = -32003


class PushNotifier:
    def __init__(self, enabled: bool = False, allowed_hosts: list[str] | None = None, timeout: float = 10):
        self.enabled = enabled
        self.allowed_hosts = allowed_hosts or []
        self.timeout = timeout
        self.configs: dict[str, dict[str, dict]] = {}  # task id -> config id -> config
        self._http: aiohttp.ClientSession | None = None
        self._pending: set[asyncio.Task] = set()

    def check_allowed(self, url: str) -> None:
        if not self.enabled:
            raise RpcError(PUSH_NOT_SUPPORTED, "Push notifications are turned off on this server")
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise RpcError(-32602, "push notification url must be an http(s) URL")
        if self.allowed_hosts and not any(fnmatch.fnmatch(parsed.hostname, h) for h in self.allowed_hosts):
            raise RpcError(-32602, f"push notifications to {parsed.hostname} are not allowed")

    def set(self, task_id: str, config: dict, version: str) -> dict:
        self.check_allowed(config.get("url"))
        config = {**config, "id": config.get("id") or uuid.uuid4().hex}
        self.configs.setdefault(task_id, {})[config["id"]] = {"config": config, "version": version}
        return config

    def get(self, task_id: str, config_id: str | None = None) -> dict:
        configs = self.configs.get(task_id) or {}
        if not configs:
            raise RpcError(-32001, "No push notification config for this task")
        entry = configs.get(config_id) if config_id else next(iter(configs.values()))
        if entry is None:
            raise RpcError(-32001, "Push notification config not found")
        return entry["config"]

    def list(self, task_id: str) -> list[dict]:
        return [e["config"] for e in (self.configs.get(task_id) or {}).values()]

    def delete(self, task_id: str, config_id: str) -> None:
        (self.configs.get(task_id) or {}).pop(config_id, None)

    def notify(self, task: dict) -> None:
        """Send the task to every webhook registered for it (in the background)."""
        for entry in list((self.configs.get(task["id"]) or {}).values()):
            job = asyncio.create_task(self._deliver(entry, task))
            self._pending.add(job)
            job.add_done_callback(self._pending.discard)

    def forget(self, task_id: str) -> None:
        self.configs.pop(task_id, None)

    async def _deliver(self, entry: dict, task: dict) -> None:
        config = entry["config"]
        body = result_to_v1(task) if entry["version"] == "1.0" else task
        headers = {}
        if config.get("token"):
            headers["X-A2A-Notification-Token"] = config["token"]
        auth = config.get("authentication") or {}
        if auth.get("credentials"):
            schemes = auth.get("schemes") or [auth.get("scheme") or "Bearer"]
            headers["Authorization"] = f"{schemes[0]} {auth['credentials']}"
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(trust_env=True)
        for attempt in range(3):
            try:
                async with self._http.post(config["url"], json=body, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status < 300:
                        return
                    log.info("push to %s answered HTTP %s", config["url"], resp.status)
            except Exception as exc:
                log.info("push to %s failed: %s", config["url"], exc)
            await asyncio.sleep(0.5 * 2 ** attempt)

    async def close(self) -> None:
        if self._pending:
            await asyncio.wait(self._pending, timeout=5)
        if self._http and not self._http.closed:
            await self._http.close()
