"""A minimal A2A v0.3 client: read an agent card, send or stream messages."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp

from kiro_a2a import tracing
from kiro_a2a.auth import AuthConfig, Authenticator, AuthError, TlsConfig
from kiro_a2a.protocol import V03_TO_V1_METHODS, message_to_v1, pick_interface, result_from_v1

WAITING_STATES = {"submitted", "working"}

# called with (text, kind); kind is the update's metadata.kind (thought, plan, tool_call, ...) or None
ProgressCallback = Callable[[str, str | None], Awaitable[None]]


class A2AError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code  # JSON-RPC error code from the agent, if it sent one


@dataclass
class Reply:
    text: str
    context_id: str | None
    task_id: str | None
    state: str
    metadata: dict = field(default_factory=dict)  # task metadata: stopReason, durationMs, toolCalls, usage


def parts_text(parts) -> str:
    """Readable text of message parts. Data parts are shown only when there is
    no text to read: next to text they are machine-readable extras (a permission
    request, a diff's raw contents) that would only clutter the answer."""
    parts = [p for p in parts or [] if isinstance(p, dict)]
    has_text = any(isinstance(p.get("text"), str) and p["text"].strip() for p in parts)
    out = []
    for part in parts:
        if isinstance(part.get("text"), str):
            out.append(part["text"])
        elif "data" in part and has_text:
            continue
        elif "data" in part:
            out.append(json.dumps(part["data"], indent=2, ensure_ascii=False))
        elif "file" in part:
            file = part["file"] or {}
            out.append(f"[file: {file.get('name') or file.get('uri') or 'attachment'}]")
    return "\n".join(out)


def reply_from_result(result: dict) -> Reply:
    """Accept either a Task or a Message as the result of message/send."""
    if result.get("kind") == "message" or ("parts" in result and "status" not in result):
        return Reply(parts_text(result.get("parts")), result.get("contextId"), result.get("taskId"), "completed")
    status = result.get("status") or {}
    # The answer first, file diffs after it.
    artifacts = sorted(result.get("artifacts") or [], key=lambda a: str(a.get("name", "")).startswith("diff:"))
    texts = [parts_text(a.get("parts")) for a in artifacts]
    text = "\n\n".join(t for t in texts if t)
    status_text = parts_text((status.get("message") or {}).get("parts"))
    if not text or status.get("state") not in ("completed", None):
        text = "\n\n".join(t for t in (text, status_text) if t)
    return Reply(text, result.get("contextId"), result.get("id"), status.get("state", "unknown"), result.get("metadata") or {})


class _StreamState:
    """Folds message/stream events into one Task-shaped dict."""

    def __init__(self, on_progress: ProgressCallback | None):
        self.on_progress = on_progress
        self.task: dict = {"kind": "task", "status": {"state": "submitted"}}
        self.artifacts: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.message: dict | None = None
        self.done = False
        self._thought: list[str] = []

    async def feed(self, result: dict) -> None:
        kind = result.get("kind")
        if kind == "message":
            self.message, self.done = result, True
        elif kind == "task":
            self.task.update({k: result[k] for k in ("id", "contextId", "status") if k in result})
            for artifact in result.get("artifacts") or []:
                self.artifacts[artifact.get("artifactId", "")] = parts_text(artifact.get("parts"))
                self.names[artifact.get("artifactId", "")] = artifact.get("name", "")
        elif kind == "artifact-update":
            artifact = result.get("artifact") or {}
            key, chunk = artifact.get("artifactId", ""), parts_text(artifact.get("parts"))
            self.names[key] = artifact.get("name", self.names.get(key, ""))
            self.artifacts[key] = self.artifacts.get(key, "") + chunk if result.get("append") else chunk
        elif kind == "status-update":
            self.task["id"] = result.get("taskId", self.task.get("id"))
            self.task["contextId"] = result.get("contextId", self.task.get("contextId"))
            status = result.get("status") or {}
            self.task["status"] = status
            text = parts_text((status.get("message") or {}).get("parts"))
            if status.get("state") in WAITING_STATES and text:
                await self._progress(text, (result.get("metadata") or {}).get("kind"))
            if result.get("final") or status.get("state") not in WAITING_STATES:
                self.done = True
        if self.done:
            await self.flush()

    async def _progress(self, text: str, kind: str | None) -> None:
        # Thoughts often arrive a few words at a time; send them as one line.
        if kind == "thought":
            self._thought.append(text)
            return
        await self.flush()
        if self.on_progress:
            await self.on_progress(text, kind)

    async def flush(self) -> None:
        if self._thought and self.on_progress:
            await self.on_progress("Thinking: " + "".join(self._thought).strip(), "thought")
        self._thought = []

    def result(self) -> dict:
        if self.message is not None:
            return self.message
        artifacts = [{"artifactId": k, "name": self.names.get(k, ""), "parts": [{"kind": "text", "text": v}]}
                     for k, v in self.artifacts.items()]
        return {**self.task, "artifacts": artifacts}


class A2AClient:
    def __init__(
        self,
        url: str,
        name: str | None = None,
        headers: dict | None = None,
        timeout: float = 600,
        auth: AuthConfig | None = None,
        tls: TlsConfig | None = None,
        card_key: str | None = None,
    ):
        self.url = url
        # public key (PEM file) or shared secret the agent card must be signed with
        self.card_key = card_key
        self.wire = "0.3"  # A2A version spoken with this agent, set from its card
        self.name = name
        self.headers = headers or {}
        self.timeout = timeout
        self.ssl = (tls or TlsConfig()).ssl_context()
        self.auth = Authenticator(auth or AuthConfig(), ssl_context=self.ssl)
        self.card: dict | None = None

    @classmethod
    def from_config(cls, entry: dict, shared_headers: dict | None = None, timeout: float = 600) -> A2AClient:
        """Build a client from one entry of an agents config file."""
        headers = {**(shared_headers or {}), **{k: os.path.expandvars(v) for k, v in (entry.get("headers") or {}).items()}}
        return cls(
            os.path.expandvars(entry["url"]),
            name=entry.get("name"),
            headers=headers,
            timeout=entry.get("timeout", timeout),
            auth=AuthConfig.from_dict(entry.get("auth")),
            tls=TlsConfig.from_dict(entry.get("tls")),
            card_key=os.path.expandvars(entry["card_key"]) if entry.get("card_key") else None,
        )

    async def request_headers(self, http: aiohttp.ClientSession, extra: dict | None = None) -> dict:
        return {**self.headers, **await self.auth.headers(http, self.card), **(extra or {})}

    def card_urls(self) -> list[str]:
        if self.url.endswith(".json"):
            return [self.url]
        base = self.url.rstrip("/")
        return [f"{base}/.well-known/agent-card.json", f"{base}/.well-known/agent.json"]

    @property
    def rpc_url(self) -> str:
        if self.card:
            url, _ = pick_interface(self.card)
            if url:
                return url
        return self.url.rsplit("/.well-known/", 1)[0]

    def _use_card(self, card: dict) -> dict:
        if self.card_key:
            from kiro_a2a.card_signing import load_key, verify_card

            try:
                verify_card(card, load_key(self.card_key))
            except ValueError as exc:
                raise A2AError(f"Refusing agent at {self.url}: {exc}") from None
        self.card = card
        self.wire = pick_interface(card)[1]
        return card

    @property
    def streams(self) -> bool:
        return bool(((self.card or {}).get("capabilities") or {}).get("streaming"))

    async def fetch_card(self, http: aiohttp.ClientSession) -> dict:
        """Cards are normally public. If the card itself needs auth, that only
        works when auth.type is set explicitly (auto needs the card first)."""
        last_error = None
        for url in self.card_urls():
            for with_auth in (False, True):
                if with_auth and self.auth.config.type in ("auto", "none"):
                    break
                try:
                    headers = await self.request_headers(http) if with_auth else self.headers
                    async with http.get(url, headers=headers, ssl=self.ssl,
                                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            return self._use_card(await resp.json(content_type=None))
                        last_error = f"HTTP {resp.status}"
                        if resp.status not in (401, 403):
                            break
                except (AuthError, A2AError):
                    raise
                except Exception as exc:
                    last_error = str(exc) or type(exc).__name__
                    break
        raise A2AError(f"Could not load agent card from {self.url}: {last_error}")

    async def ask(
        self,
        http: aiohttp.ClientSession,
        text: str,
        context_id: str | None = None,
        task_id: str | None = None,
        on_progress: ProgressCallback | None = None,
        files: list[str] | None = None,
    ) -> Reply:
        """Send a message and wait for the answer. With `on_progress` and an
        agent that streams, working updates are reported as they happen."""
        if self.card is None:
            await self.fetch_card(http)
        params = {
            "message": _user_message(text, context_id, task_id, files),
            "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain"]},
        }
        deadline = time.monotonic() + self.timeout
        try:
            result = await self._send_or_stream(http, params, on_progress)
        except A2AError as exc:
            if not task_id or exc.code not in (-32602, -32002, -32004):
                raise
            # An answer to a question that is no longer open: show how the task ended instead.
            try:
                task = await self._rpc(http, "tasks/get", {"id": task_id})
            except A2AError:
                raise exc from None
            state = (task.get("status") or {}).get("state")
            if state in WAITING_STATES or state == "input-required":
                raise
            reply = reply_from_result(task)
            reply.text = (f"(This answer arrived after the question had closed: the task already ended as "
                          f"'{state}'. Its result is below. Send a new message without task_id to start again.)\n\n"
                          + reply.text)
            return reply
        # Some agents answer right away with a running task; poll until it settles.
        while result.get("kind") == "task" and (result.get("status") or {}).get("state") in WAITING_STATES:
            if time.monotonic() > deadline or not result.get("id"):
                break
            await asyncio.sleep(1)
            result = await self._rpc(http, "tasks/get", {"id": result["id"]})
        return reply_from_result(result)

    async def _send_or_stream(self, http: aiohttp.ClientSession, params: dict, on_progress) -> dict:
        if on_progress and self.streams:
            result = await self._stream(http, params, on_progress)
            if result.get("kind") == "task" and result.get("id") and not result.get("metadata"):
                try:  # the stream doesn't carry the task's final metadata; fetch it
                    full = await self._rpc(http, "tasks/get", {"id": result["id"], "historyLength": 0})
                    result["metadata"] = full.get("metadata") or {}
                except Exception:
                    pass
        else:
            result = await self._rpc(http, "message/send", params)
        return result

    async def send(self, http: aiohttp.ClientSession, text: str, context_id: str | None = None) -> Reply:
        return await self.ask(http, text, context_id)

    async def _post(self, http: aiohttp.ClientSession, method: str, params: dict, extra: dict | None = None):
        """POST a JSON-RPC call. On 401 with OAuth, get a fresh token and retry once."""
        if self.wire == "1.0":
            method = V03_TO_V1_METHODS.get(method, method)
            if isinstance(params.get("message"), dict):
                params = {**params, "message": message_to_v1(params["message"])}
            extra = {**(extra or {}), "A2A-Version": "1.0"}
        body = _rpc_body(method, params)
        for attempt in (1, 2):
            headers = tracing.inject(await self.request_headers(http, extra))
            resp = await http.post(
                self.rpc_url, json=body, headers=headers, ssl=self.ssl,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
            if resp.status == 401 and attempt == 1 and (self.auth._resolved or {}).get("type") == "oauth2":
                resp.release()
                self.auth.invalidate()
                continue
            if resp.status != 200:
                text = await resp.text()
                resp.release()
                hint = " (check the agent's auth settings)" if resp.status in (401, 403) else ""
                raise A2AError(f"{method} failed: HTTP {resp.status}{hint} {text[:300]}")
            return resp

    async def _rpc(self, http: aiohttp.ClientSession, method: str, params: dict) -> dict:
        async with await self._post(http, method, params) as resp:
            return result_from_v1(_rpc_result(method, await resp.json(content_type=None)))

    async def events(self, http: aiohttp.ClientSession, params: dict):
        """Call message/stream and yield each result object as it arrives."""
        async with await self._post(http, "message/stream", params, {"Accept": "text/event-stream"}) as resp:
            if not resp.headers.get("Content-Type", "").startswith("text/event-stream"):
                yield result_from_v1(_rpc_result("message/stream", await resp.json(content_type=None)))
                return
            buffer, data_lines = b"", []
            async for chunk in resp.content.iter_any():
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8").rstrip("\r")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].removeprefix(" "))
                    elif not line and data_lines:
                        event = json.loads("\n".join(data_lines))
                        data_lines = []
                        yield result_from_v1(_rpc_result("message/stream", event))
            if data_lines:
                yield result_from_v1(_rpc_result("message/stream", json.loads("\n".join(data_lines))))

    async def _stream(self, http: aiohttp.ClientSession, params: dict, on_progress: ProgressCallback) -> dict:
        state = _StreamState(on_progress)
        events = self.events(http, params)
        try:
            async for result in events:
                await state.feed(result)
                if state.done:
                    break
        finally:
            await events.aclose()
        await state.flush()
        return state.result()


def file_part(path: str) -> dict:
    """An A2A file part with the file's bytes."""
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode()
    return {"kind": "file", "file": {"name": os.path.basename(path), "mimeType": mime, "bytes": data}}


def _user_message(text: str, context_id: str | None, task_id: str | None, files: list[str] | None = None) -> dict:
    message = {
        "kind": "message",
        "role": "user",
        "messageId": uuid.uuid4().hex,
        "parts": [{"kind": "text", "text": text}, *(file_part(f) for f in files or [])],
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    return message


def _rpc_body(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}


def _rpc_result(method: str, data: dict) -> dict:
    if "error" in data:
        err = data["error"] or {}
        raise A2AError(f"{method} failed: {err.get('message')} (code {err.get('code')})", err.get("code"))
    return data.get("result") or {}
