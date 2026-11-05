"""A2A server in front of an ACP agent (Kiro CLI or another ACP agent).

Speaks A2A 1.0 and A2A 0.3 JSON-RPC on the same URL (the method name tells
which), streams over SSE, and serves the agent card at
/.well-known/agent-card.json.

Mapping:
  A2A contextId        -> one ACP session (follow-ups continue it)
  A2A message          -> one ACP session/prompt turn, tracked as an A2A task
  text / files / data  -> ACP prompt blocks (see attachments.py)
  answer text          -> artifact "response", streamed in chunks
  file edits (diffs)   -> one artifact per file, "diff: <path>"
  thoughts, tool calls, plan, permission decisions
                       -> working status updates, metadata.kind says which
  permission requests  -> automatic policy, or input-required ("ask" mode)
  tasks/cancel         -> ACP session/cancel
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import hmac
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from aiohttp import web

from kiro_a2a import __version__, tracing
from kiro_a2a.acp import AcpAgent, pick_option
from kiro_a2a.attachments import to_prompt_blocks
from kiro_a2a.jsonrpc import RpcError
from kiro_a2a.protocol import (
    STATE_FROM_V1,
    V1_METHODS,
    card_to_v1,
    message_from_v1,
    result_to_v1,
    task_to_v1,
)
from kiro_a2a.push import PushNotifier
from kiro_a2a.store import MemoryTaskStore

log = logging.getLogger(__name__)

TERMINAL_STATES = {"completed", "canceled", "failed", "rejected"}

# A2A error codes
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
UNSUPPORTED_OPERATION = -32004


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def message_text(message: dict) -> str:
    """Join the text and data parts of an A2A message."""
    texts = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif part.get("kind") == "data" or "data" in part:
            texts.append(json.dumps(part.get("data"), ensure_ascii=False))
    return "\n".join(texts)


def _has_content(message: dict) -> bool:
    for part in message.get("parts") or []:
        if isinstance(part, dict) and (part.get("file") or part.get("data") is not None or (part.get("text") or "").strip()):
            return True
    return False


def _content_text(block) -> str:
    if isinstance(block, list):
        return "".join(_content_text(b) for b in block)
    if not isinstance(block, dict):
        return ""
    if block.get("type", "text") == "text":
        return block.get("text") or ""
    if block.get("type") == "resource_link":
        return block.get("uri") or ""
    if block.get("type") == "content":  # tool call content wraps a content block
        return _content_text(block.get("content"))
    return ""


def describe_update(update: dict) -> tuple[str | None, str]:
    """Turn a non-answer ACP update into (metadata kind, short readable text)."""
    kind = update.get("sessionUpdate")
    if kind == "agent_thought_chunk":
        return "thought", _content_text(update.get("content"))
    if kind == "tool_call":
        title = update.get("title") or "tool"
        return "tool_call", f"{title} [{update.get('kind', 'other')}] {update.get('status', 'pending')}"
    if kind == "tool_call_update" and update.get("status"):
        name = update.get("title") or update.get("toolCallId") or "tool"
        return "tool_call_update", f"{name}: {update['status']}"
    if kind == "plan":
        lines = [f"[{e.get('status', 'pending')}] {e.get('content', '')}" for e in update.get("entries") or []]
        return "plan", "\n".join(lines)
    if kind == "permission":
        tool = update.get("toolCall") or {}
        verdict = "Allowed" if update.get("allowed") else "Denied"
        return "permission", f"{verdict}: {tool.get('title') or 'tool'} [{tool.get('kind', 'other')}]"
    return None, ""


def unified_diff(path: str, old: str | None, new: str) -> str:
    lines = difflib.unified_diff(
        (old or "").splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="/dev/null" if old is None else f"a/{path}", tofile=f"b/{path}",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


ALLOW_WORDS = {"allow", "yes", "y", "approve", "approved", "ok", "okay", "sure", "always"}
DENY_WORDS = {"deny", "no", "n", "reject", "refuse", "cancel", "stop"}


def choose_permission(options: list[dict], message: dict) -> dict:
    """Read the caller's answer to a permission question. Accepts an option id
    (as text or as a data part {"optionId": ...}), an option name, or plain
    words like allow / deny / always. Anything unclear counts as deny."""
    ids = {o.get("optionId"): o for o in options}
    for part in message.get("parts") or []:
        data = part.get("data") if isinstance(part, dict) else None
        if isinstance(data, dict) and data.get("optionId") in ids:
            return {"outcome": "selected", "optionId": data["optionId"]}
    text = message_text(message).strip()
    for option in options:
        if text and text in (option.get("optionId"), option.get("name")):
            return {"outcome": "selected", "optionId": option["optionId"]}
    words = set(re.findall(r"[a-z]+", text.lower()))
    allow = bool(words & ALLOW_WORDS) and not words & DENY_WORDS
    return pick_option(options, allow, prefer_always="always" in words)


def _answer_request_id(message: dict) -> str | None:
    for part in message.get("parts") or []:
        data = part.get("data") if isinstance(part, dict) else None
        if isinstance(data, dict) and data.get("requestId"):
            return data["requestId"]
    return None


class _Run:
    """A task that is currently executing."""

    def __init__(self, task: dict, message: dict, trace_parent=None):
        self.task = task
        self.message = message
        self.trace_parent = trace_parent
        self.session_id: str | None = None
        self.cancel_requested = False
        self.subscribers: list[asyncio.Queue] = []
        self.runner: asyncio.Task | None = None
        self.artifact_id = _new_id()
        self.has_answer = False
        self.tool_calls: set[str] = set()
        # set when the task is finished or waiting for the caller (input-required)
        self.settled = asyncio.Event()
        self.pending_permission: tuple[dict, asyncio.Future, str] | None = None
        self.permission_timer: asyncio.TimerHandle | None = None


class Bridge:
    def __init__(
        self,
        agent: AcpAgent,
        name: str = "Kiro",
        description: str = "Kiro coding agent, reachable over A2A.",
        public_url: str = "http://localhost:8765/",
        api_key: str | None = None,
        oauth: OAuthValidator | None = None,
        prompt_timeout: float = 900,
        max_parallel: int = 4,
        permission_timeout: float = 600,
        store=None,
        push: PushNotifier | None = None,
        signer=None,
    ):
        self.agent = agent
        self.name = name
        self.description = description
        self.public_url = public_url
        self.api_key = api_key
        self.oauth = oauth
        self.prompt_timeout = prompt_timeout
        self.permission_timeout = permission_timeout
        self.slots = asyncio.Semaphore(max_parallel)
        self.store = store or MemoryTaskStore()
        self.push = push or PushNotifier()
        self.signer = signer
        self.runs: dict[str, _Run] = {}
        self.session_runs: dict[str, _Run] = {}
        self.contexts: dict[str, tuple[str, int]] = {}
        self.context_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        agent.permission_handler = self._ask_permission

    # ---- agent card -----------------------------------------------------

    def agent_card(self, version: str = "0.3") -> dict:
        card = {
            "protocolVersion": "0.3.0",
            "name": self.name,
            "description": self.description,
            "url": self.public_url,
            "preferredTransport": "JSONRPC",
            "supportedInterfaces": [
                {"url": self.public_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
                {"url": self.public_url, "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
            ],
            "version": __version__,
            "capabilities": {"streaming": True, "pushNotifications": self.push.enabled},
            "defaultInputModes": ["text/plain", "application/json", "text/*", "image/*"],
            "defaultOutputModes": ["text/plain", "text/x-diff"],
            "skills": [
                {"id": "review", "name": "Code review", "tags": ["code", "review"],
                 "description": "Reviews code or a diff for bugs, security problems and style."},
                {"id": "explain", "name": "Explain code", "tags": ["code"],
                 "description": "Explains how a file, module or repository works."},
                {"id": "change", "name": "Change code", "tags": ["code", "edit"],
                 "description": "Writes or edits code in the workspace (when edits are allowed)."},
                {"id": "test", "name": "Write tests", "tags": ["code", "tests"],
                 "description": "Writes or suggests tests for given code."},
            ],
        }
        schemes, security = {}, []
        if self.oauth:
            schemes["oauth"] = {"type": "openIdConnect", "openIdConnectUrl": self.oauth.discovery_url}
            security.append({"oauth": list(self.oauth.scopes)})
        if self.api_key:
            schemes["bearer"] = {"type": "http", "scheme": "bearer"}
            security.append({"bearer": []})
        if schemes:
            # Each entry in `security` is an alternative.
            card["securitySchemes"] = schemes
            card["security"] = security
        if version.startswith("1"):
            card = card_to_v1(card, self.public_url)
            card["supportedInterfaces"].append(
                {"url": self.public_url, "protocolBinding": "JSONRPC", "protocolVersion": "0.3"})
        return self.signer.sign(card) if self.signer else card

    # ---- JSON-RPC methods -----------------------------------------------

    async def send(self, params: dict, version: str = "0.3", trace_parent=None) -> dict:
        run = self._start(params.get("message"), params.get("configuration") or {}, version, trace_parent)
        blocking = (params.get("configuration") or {}).get("blocking", True)
        if blocking:
            # Returns when the task ends or needs input. The task keeps
            # going even if the HTTP client goes away.
            await run.settled.wait()
        return copy.deepcopy(run.task)

    def get(self, params: dict) -> dict:
        task = self.store.get(_task_id(params))
        if task is None:
            raise RpcError(TASK_NOT_FOUND, "Task not found")
        task = copy.deepcopy(task)
        limit = params.get("historyLength")
        if isinstance(limit, int) and limit >= 0:
            task["history"] = task.get("history", [])[-limit:] if limit else []
        return task

    def list_tasks(self, params: dict) -> dict:
        size = max(1, min(int(params.get("pageSize") or 50), 200))
        offset = int(params.get("pageToken") or 0)
        state = params.get("status") or params.get("state")
        state = STATE_FROM_V1.get(state, state)
        tasks = self.store.list(params.get("contextId"), state, size + 1, offset)
        more = len(tasks) > size
        return {"tasks": [copy.deepcopy(t) for t in tasks[:size]], "nextPageToken": str(offset + size) if more else ""}

    async def cancel(self, params: dict) -> dict:
        task_id = _task_id(params)
        task = self.store.get(task_id)
        if task is None:
            raise RpcError(TASK_NOT_FOUND, "Task not found")
        run = self.runs.get(task_id)
        if run is None or task["status"]["state"] in TERMINAL_STATES:
            raise RpcError(TASK_NOT_CANCELABLE, "Task is already finished")
        run.cancel_requested = True
        self._drop_permission(run)
        if run.session_id:
            await self.agent.cancel(run.session_id)
        try:
            await asyncio.wait_for(asyncio.shield(run.runner), timeout=15)
        except asyncio.TimeoutError:
            log.warning("agent did not stop task %s within 15s", task_id)
        return copy.deepcopy(run.task)

    def push_config(self, method: str, params: dict, version: str):
        task_id = params.get("taskId") or params.get("id")
        if method.endswith("/set"):
            if self.store.get(task_id) is None:
                raise RpcError(TASK_NOT_FOUND, "Task not found")
            config = params.get("pushNotificationConfig") or params.get("config") or {
                k: v for k, v in params.items() if k not in ("taskId", "tenant")}
            saved = self.push.set(task_id, config, version)
            return {"taskId": task_id, "pushNotificationConfig": saved}
        if method.endswith("/get"):
            config_id = params.get("pushNotificationConfigId") or (params.get("id") if params.get("taskId") else None)
            return {"taskId": task_id, "pushNotificationConfig": self.push.get(task_id, config_id)}
        if method.endswith("/list"):
            return [{"taskId": task_id, "pushNotificationConfig": c} for c in self.push.list(task_id)]
        self.push.delete(task_id, params.get("pushNotificationConfigId") or params.get("id"))
        return None

    # ---- task execution -------------------------------------------------

    def _start(self, message, configuration: dict | None = None, version: str = "0.3", trace_parent=None) -> _Run:
        """Create the task and schedule it. Synchronous on purpose: callers can
        subscribe to events before the first one is published."""
        if not isinstance(message, dict):
            raise RpcError(-32602, "params.message is required")
        if message.get("taskId"):
            if message["taskId"] not in self.store:
                raise RpcError(TASK_NOT_FOUND, "Task not found. To start a new task, send the message without taskId.")
            run = self.runs.get(message["taskId"])
            if run is None or run.pending_permission is None:
                state = self.store.get(message["taskId"])["status"]["state"]
                raise RpcError(-32602, f"This task is not waiting for input (it is {state}). "
                                       "Send the message without taskId to start a new task.")
            self._resume(run, message)
            return run
        if not _has_content(message):
            raise RpcError(-32602, "message has no content")
        configuration = configuration or {}
        push_config = configuration.get("pushNotificationConfig") or configuration.get("taskPushNotificationConfig")
        if push_config:
            self.push.check_allowed(push_config.get("url"))
        context_id = message.get("contextId") or _new_id()
        task_id = _new_id()
        user_message = {**message, "kind": "message", "contextId": context_id, "taskId": task_id}
        task = {
            "kind": "task",
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "submitted", "timestamp": _now()},
            "artifacts": [],
            "history": [user_message],
            "metadata": {},
        }
        self.store.put(task)
        if push_config:
            self.push.set(task_id, push_config, version)
        run = _Run(task, user_message, trace_parent)
        self.runs[task_id] = run
        run.runner = asyncio.create_task(self._execute(run))
        return run

    async def _execute(self, run: _Run) -> None:
        task = run.task
        context_id = task["contextId"]
        started = time.monotonic()
        with tracing.span("kiro_a2a.task", run.trace_parent, **{"a2a.task_id": task["id"], "a2a.context_id": context_id}):
            try:
                async with self.context_locks[context_id]:
                    if run.cancel_requested:
                        self._finish(run, "canceled")
                        return
                    self._set_status(run, "working")
                    run.session_id = await self._session_for(context_id)
                    self.session_runs[run.session_id] = run
                    blocks = to_prompt_blocks(run.message, self.agent.prompt_capabilities)
                    async with self.slots:
                        try:
                            result = await asyncio.wait_for(
                                self.agent.prompt(run.session_id, blocks, lambda update: self._on_update(run, update)),
                                self.prompt_timeout,
                            )
                        except asyncio.TimeoutError:
                            await self.agent.cancel(run.session_id)
                            self._finish(run, "failed", f"The agent did not finish within {self.prompt_timeout:g}s.")
                            return
                    stop_reason = result["stopReason"]
                    task["metadata"].update({
                        "stopReason": stop_reason,
                        "durationMs": int((time.monotonic() - started) * 1000),
                        "toolCalls": len(run.tool_calls),
                    })
                    if result.get("usage"):
                        task["metadata"]["usage"] = result["usage"]
                    if run.cancel_requested or stop_reason == "cancelled":
                        self._finish(run, "canceled")
                    elif stop_reason == "refusal":
                        self._finish(run, "rejected", "The agent refused this request.")
                    else:
                        self._finish(run, "completed")
            except Exception as exc:
                log.exception("task %s failed", task["id"])
                self._finish(run, "failed", f"Agent error: {exc}")
            finally:
                self.runs.pop(task["id"], None)
                if self.session_runs.get(run.session_id) is run:
                    del self.session_runs[run.session_id]

    async def _session_for(self, context_id: str) -> str:
        await self.agent.ensure_started()
        known = self.contexts.get(context_id)
        if known and known[1] == self.agent.generation:
            return known[0]
        # The agent restarted since this session was made: try to reopen it.
        if known and await self.agent.load_session(known[0]):
            self.contexts[context_id] = (known[0], self.agent.generation)
            return known[0]
        session_id, generation = await self.agent.new_session()
        self.contexts[context_id] = (session_id, generation)
        return session_id

    # ---- "ask" permission mode ------------------------------------------

    async def _ask_permission(self, session_id: str, params: dict) -> dict:
        """Pause the task as input-required until the caller answers."""
        run = self.session_runs.get(session_id)
        if run is None:
            return {"outcome": "cancelled"}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request_id = _new_id()
        run.pending_permission = (params, future, request_id)
        run.permission_timer = loop.call_later(self.permission_timeout, self._expire_permission, run)

        tool = params.get("toolCall") or {}
        options = params.get("options") or []
        listed = "\n".join(f"- {o.get('name')} ({o.get('optionId')})" for o in options)
        text = (
            f"The agent wants to use a tool: {tool.get('title') or 'tool'} [{tool.get('kind', 'other')}].\n"
            f"Reply 'allow' or 'deny' on this task (taskId {run.task['id']}), or send an option id:\n{listed}"
        )
        data = {
            "type": "permission_request",
            "requestId": request_id,
            "toolCall": {k: tool.get(k) for k in ("toolCallId", "title", "kind")},
            "options": options,
        }
        self._set_status(run, "input-required", text, {"kind": "permission_request"}, final=True, data=data)
        self._end_streams(run)
        run.settled.set()
        return await future

    def _resume(self, run: _Run, message: dict) -> None:
        params, future, request_id = run.pending_permission
        answered = _answer_request_id(message)
        if answered and answered != request_id:
            raise RpcError(-32602, "This answer is for an earlier question that is no longer open.")
        self._drop_permission(run, keep_future=True)
        outcome = choose_permission(params.get("options") or [], message)
        run.task["history"].append({**message, "kind": "message", "contextId": run.task["contextId"]})
        run.settled.clear()
        verdict = f"selected {outcome['optionId']}" if outcome.get("optionId") else "cancelled"
        self._set_status(run, "working", f"Permission answer: {verdict}", {"kind": "permission"})
        if not future.done():
            future.set_result(outcome)

    def _expire_permission(self, run: _Run) -> None:
        if run.pending_permission is None:
            return
        self._drop_permission(run)
        run.settled.clear()
        self._set_status(run, "working", "No answer to the permission request in time; the tool was not run.",
                         {"kind": "permission"})

    def _drop_permission(self, run: _Run, keep_future: bool = False) -> None:
        """Forget the open permission question; unless keep_future, answer it with cancel."""
        if run.pending_permission is None:
            return
        _, future, _ = run.pending_permission
        run.pending_permission = None
        if run.permission_timer:
            run.permission_timer.cancel()
            run.permission_timer = None
        if not keep_future and not future.done():
            future.set_result({"outcome": "cancelled"})

    # ---- events -----------------------------------------------------------

    def _on_update(self, run: _Run, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            chunk = _content_text(update.get("content"))
            if chunk:
                self._answer_chunk(run, chunk)
            return
        if kind in ("tool_call", "tool_call_update"):
            if update.get("toolCallId"):
                run.tool_calls.add(update["toolCallId"])
            for item in update.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "diff" and item.get("path"):
                    self._diff_artifact(run, update.get("toolCallId") or "", item)
        meta_kind, text = describe_update(update)
        if meta_kind and text:
            self._set_status(run, "working", text, {"kind": meta_kind, "update": update})

    def _answer_chunk(self, run: _Run, chunk: str) -> None:
        task = run.task
        if not run.has_answer:
            task["artifacts"].insert(0, {"artifactId": run.artifact_id, "name": "response",
                                         "parts": [{"kind": "text", "text": ""}]})
        artifact = next(a for a in task["artifacts"] if a["artifactId"] == run.artifact_id)
        artifact["parts"][0]["text"] += chunk
        self._publish(run, {
            "kind": "artifact-update",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "artifact": {"artifactId": run.artifact_id, "name": "response", "parts": [{"kind": "text", "text": chunk}]},
            "append": run.has_answer,
            "lastChunk": False,
        })
        run.has_answer = True

    def _diff_artifact(self, run: _Run, tool_call_id: str, diff: dict) -> None:
        path = diff["path"]
        artifact = {
            "artifactId": f"diff-{uuid.uuid5(uuid.NAMESPACE_URL, tool_call_id + path).hex}",
            "name": f"diff: {path}",
            "description": f"Change to {path}",
            "parts": [
                {"kind": "text", "text": unified_diff(path, diff.get("oldText"), diff.get("newText") or ""),
                 "metadata": {"mimeType": "text/x-diff"}},
                {"kind": "data", "data": {"path": path, "oldText": diff.get("oldText"), "newText": diff.get("newText")}},
            ],
        }
        artifacts = run.task["artifacts"]
        artifacts[:] = [a for a in artifacts if a["artifactId"] != artifact["artifactId"]] + [artifact]
        self._publish(run, {"kind": "artifact-update", "taskId": run.task["id"], "contextId": run.task["contextId"],
                            "artifact": artifact, "append": False, "lastChunk": True})

    def _set_status(
        self, run: _Run, state: str, text: str | None = None, metadata: dict | None = None,
        final: bool = False, data: dict | None = None,
    ):
        task = run.task
        changed = task["status"]["state"] != state
        status: dict = {"state": state, "timestamp": _now()}
        if text:
            parts = [{"kind": "text", "text": text}]
            if data:
                parts.append({"kind": "data", "data": data})
            status["message"] = {
                "kind": "message",
                "role": "agent",
                "messageId": _new_id(),
                "parts": parts,
                "taskId": task["id"],
                "contextId": task["contextId"],
            }
            if metadata:
                status["message"]["metadata"] = metadata
        task["status"] = status
        event = {"kind": "status-update", "taskId": task["id"], "contextId": task["contextId"],
                 "status": status, "final": final}
        if metadata:
            event["metadata"] = metadata
        self._publish(run, event)
        if changed or final:
            self.store.put(task)
            self.push.notify(copy.deepcopy(task))

    def _finish(self, run: _Run, state: str, error: str | None = None) -> None:
        self._drop_permission(run)
        self._set_status(run, state, error, final=True)
        self._end_streams(run)
        run.settled.set()

    def _end_streams(self, run: _Run) -> None:
        for queue in run.subscribers:
            queue.put_nowait(None)
        run.subscribers.clear()

    def _publish(self, run: _Run, event: dict) -> None:
        for queue in run.subscribers:
            queue.put_nowait(event)

    # ---- HTTP -----------------------------------------------------------

    async def authorized(self, request: web.Request) -> bool:
        if not self.api_key and not self.oauth:
            return True
        bearer = request.headers.get("Authorization", "")
        if self.api_key and (
            hmac.compare_digest(bearer, f"Bearer {self.api_key}")
            or hmac.compare_digest(request.headers.get("X-API-Key", ""), self.api_key)
        ):
            return True
        if self.oauth and bearer.lower().startswith("bearer "):
            try:
                await self.oauth.verify(bearer[7:].strip())
                return True
            except Exception as exc:
                log.info("rejected token: %s", exc)
        return False

    async def handle_card(self, request: web.Request) -> web.Response:
        version = request.headers.get("A2A-Version") or request.query.get("A2A-Version") or "0.3"
        return web.json_response(self.agent_card(version))

    async def handle_rpc(self, request: web.Request) -> web.StreamResponse:
        if not await self.authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401,
                                     headers={"WWW-Authenticate": 'Bearer realm="a2a"'})
        try:
            body = await request.json()
        except Exception:
            return _rpc_error(None, RpcError(-32700, "Invalid JSON"))
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return _rpc_error(body.get("id") if isinstance(body, dict) else None, RpcError(-32600, "Invalid request"))

        rpc_id, method, params = body.get("id"), body["method"], body.get("params") or {}
        version = "1.0" if method in V1_METHODS else "0.3"
        method = V1_METHODS.get(method, method)
        if version == "1.0" and isinstance(params.get("message"), dict):
            params = {**params, "message": message_from_v1(params["message"])}
        parent = tracing.extract(request.headers)
        try:
            if method == "message/stream":
                run = self._start(params.get("message"), params.get("configuration") or {}, version, parent)
                return await self._stream(request, rpc_id, run, version)
            if method == "tasks/resubscribe":
                return await self._resubscribe(request, rpc_id, params, version)
            if method == "message/send":
                result = await self.send(params, version, parent)
            elif method == "tasks/get":
                result = self.get(params)
            elif method == "tasks/cancel":
                result = await self.cancel(params)
            elif method == "tasks/list":
                result = self.list_tasks(params)
            elif method.startswith("tasks/pushNotificationConfig/"):
                result = self.push_config(method, params, version)
            else:
                raise RpcError(-32601, f"Method not found: {body['method']}")
        except RpcError as exc:
            return _rpc_error(rpc_id, exc)
        if version == "1.0":
            result = _result_v1(method, result)
        return web.json_response({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    async def _stream(self, request: web.Request, rpc_id, run: _Run, version: str) -> web.StreamResponse:
        queue: asyncio.Queue = asyncio.Queue()
        run.subscribers.append(queue)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)

        async def emit(event: dict) -> None:
            result = result_to_v1(event) if version == "1.0" else event
            payload = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}, ensure_ascii=False)
            await response.write(f"data: {payload}\n\n".encode())

        try:
            await emit(copy.deepcopy(run.task))
            while (event := await queue.get()) is not None:
                await emit(event)
        except ConnectionResetError:
            log.info("stream client left; task %s keeps running", run.task["id"])
        finally:
            if queue in run.subscribers:
                run.subscribers.remove(queue)
        return response

    async def _resubscribe(self, request: web.Request, rpc_id, params: dict, version: str) -> web.StreamResponse:
        task_id = _task_id(params)
        run = self.runs.get(task_id)
        if run is not None and not run.settled.is_set():
            return await self._stream(request, rpc_id, run, version)
        task = self.store.get(task_id)
        if task is None:
            raise RpcError(TASK_NOT_FOUND, "Task not found")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        result = result_to_v1(task) if version == "1.0" else task
        await response.write(f"data: {json.dumps({'jsonrpc': '2.0', 'id': rpc_id, 'result': result})}\n\n".encode())
        return response


def _task_id(params: dict) -> str | None:
    value = params.get("id") or params.get("taskId") or params.get("name") or ""
    return value.rsplit("/", 1)[-1] if value else None


def _result_v1(method: str, result):
    if method == "message/send":
        return result_to_v1(result)
    if method in ("tasks/get", "tasks/cancel"):
        return task_to_v1(result)
    if method == "tasks/list":
        return {**result, "tasks": [task_to_v1(t) for t in result["tasks"]]}
    if method.startswith("tasks/pushNotificationConfig/"):
        flat = lambda e: {"taskId": e["taskId"], **e["pushNotificationConfig"]}  # noqa: E731
        if isinstance(result, list):
            return {"configs": [flat(e) for e in result]}
        return flat(result) if result else {}
    return result


class OAuthValidator:
    """Checks OAuth access tokens (JWTs) from an OpenID Connect issuer.

    Needs PyJWT with crypto support: pip install "kiro-a2a-bridge[oauth]"."""

    ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]

    def __init__(self, issuer: str, audience: str | None = None, scopes=(), jwks_url: str | None = None):
        try:
            import jwt  # noqa: F401
        except ImportError:
            raise SystemExit('OAuth token checks need PyJWT: pip install "kiro-a2a-bridge[oauth]"') from None
        self.issuer = issuer
        self.audience = audience
        self.scopes = list(scopes)
        self.jwks_url = jwks_url
        self._keys = None

    @property
    def discovery_url(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"

    def _key_client(self):
        import jwt

        if self._keys is None:
            url = self.jwks_url
            if not url:
                import urllib.request

                with urllib.request.urlopen(self.discovery_url, timeout=15) as resp:
                    url = json.load(resp)["jwks_uri"]
            self._keys = jwt.PyJWKClient(url, cache_keys=True)
        return self._keys

    def check(self, token: str) -> dict:
        import jwt

        key = self._key_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=self.ALGORITHMS, audience=self.audience, issuer=self.issuer,
            options={"require": ["exp", "iss"], "verify_aud": bool(self.audience)},
        )
        granted = claims.get("scope") or claims.get("scp") or []
        granted = set(granted.split() if isinstance(granted, str) else granted)
        missing = [s for s in self.scopes if s not in granted]
        if missing:
            raise PermissionError(f"token lacks scope(s): {', '.join(missing)}")
        return claims

    async def verify(self, token: str) -> dict:
        return await asyncio.to_thread(self.check, token)


def _rpc_error(rpc_id, exc: RpcError) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": rpc_id, "error": exc.to_dict()})


def make_app(bridge: Bridge) -> web.Application:
    app = web.Application(client_max_size=32 * 1024 * 1024)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "agentRunning": bridge.agent.alive, "activeTasks": len(bridge.runs)})

    app.router.add_get("/.well-known/agent-card.json", bridge.handle_card)
    app.router.add_get("/.well-known/agent.json", bridge.handle_card)
    app.router.add_get("/health", health)
    app.router.add_post("/", bridge.handle_rpc)
    app.router.add_post("/a2a", bridge.handle_rpc)

    async def shutdown(app: web.Application) -> None:
        await bridge.agent.close()
        await bridge.push.close()
        if hasattr(bridge.store, "close"):
            bridge.store.close()

    app.on_cleanup.append(shutdown)
    return app
