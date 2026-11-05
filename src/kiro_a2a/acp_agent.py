"""`kiro-a2a acp`: present one A2A agent as an ACP agent.

Editors that host ACP agents start this command and talk to it on
stdin/stdout. Each prompt becomes an A2A message; the A2A agent's answer,
progress and questions come back as ACP session updates.

  ACP session            -> A2A contextId (follow-ups continue the conversation)
  answer text            -> agent_message_chunk
  working updates        -> agent_thought_chunk (plan updates -> plan)
  permission question    -> session/request_permission, answer sent back to the task
  other questions        -> shown as the answer; the next prompt replies to that task
  session/cancel         -> tasks/cancel
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import aiohttp

from kiro_a2a import __version__
from kiro_a2a.a2a_client import WAITING_STATES, A2AClient, _user_message, parts_text
from kiro_a2a.auth import AuthError
from kiro_a2a.jsonrpc import Peer, RpcError, run_stdio

log = logging.getLogger(__name__)

STOP_FOR_STATE = {"completed": "end_turn", "canceled": "cancelled", "rejected": "refusal",
                  "failed": "end_turn", "input-required": "end_turn", "auth-required": "end_turn"}


@dataclass
class _Session:
    context_id: str | None = None
    task_id: str | None = None          # task of the prompt in progress
    reply_to_task: str | None = None    # task waiting for the user's answer
    cancelled: bool = False


def prompt_text(blocks: list[dict]) -> str:
    """Flatten ACP content blocks into text the A2A agent can read."""
    out = []
    for block in blocks or []:
        kind = block.get("type")
        if kind == "text":
            out.append(block.get("text", ""))
        elif kind == "resource":
            resource = block.get("resource") or {}
            if "text" in resource:
                out.append(f"File {resource.get('uri', '')}:\n```\n{resource['text']}\n```")
        elif kind == "resource_link":
            out.append(f"[file: {block.get('uri')}]")
    return "\n\n".join(t for t in out if t)


class AcpFront:
    def __init__(self, client: A2AClient):
        self.client = client
        self.peer: Peer | None = None
        self.http: aiohttp.ClientSession | None = None
        self.sessions: dict[str, _Session] = {}

    def _session_http(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(trust_env=True)
        return self.http

    async def handle(self, method: str, params: dict | None):
        params = params or {}
        if method == "initialize":
            name = self.client.name or "a2a-agent"
            try:
                card = await self.client.fetch_card(self._session_http())
                name = card.get("name") or name
            except Exception as exc:
                log.warning("could not load agent card: %s", exc)
            return {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                },
                "authMethods": [],
                "agentInfo": {"name": name, "title": name, "version": __version__},
            }
        if method == "authenticate":
            return {}
        if method == "session/new":
            session_id = uuid.uuid4().hex
            self.sessions[session_id] = _Session()
            return {"sessionId": session_id}
        if method == "session/prompt":
            return await self._prompt(params)
        raise RpcError(-32601, f"Method not found: {method}")

    def on_notification(self, method: str, params: dict | None) -> None:
        if method == "session/cancel":
            session = self.sessions.get((params or {}).get("sessionId"))
            if session:
                session.cancelled = True
                if session.task_id and self.peer:
                    self.peer._spawn(self._cancel_task(session.task_id))

    async def _cancel_task(self, task_id: str) -> None:
        try:
            await self.client._rpc(self._session_http(), "tasks/cancel", {"id": task_id})
        except Exception as exc:
            log.info("cancel of task %s: %s", task_id, exc)

    async def _say(self, session_id: str, update: dict) -> None:
        await self.peer.notify("session/update", {"sessionId": session_id, "update": update})

    async def _text(self, session_id: str, text: str, thought: bool = False) -> None:
        if text:
            kind = "agent_thought_chunk" if thought else "agent_message_chunk"
            await self._say(session_id, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

    async def _prompt(self, params: dict) -> dict:
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id)
        if session is None:
            raise RpcError(-32602, f"Unknown session: {session_id}")
        session.cancelled = False
        text = prompt_text(params.get("prompt") or [])
        message = _user_message(text, session.context_id, session.reply_to_task)
        session.reply_to_task = None
        http = self._session_http()
        try:
            if self.client.card is None:
                await self.client.fetch_card(http)
            while True:
                state, final_task = await self._run(session_id, session, http, message)
                answer = self._permission_answer(final_task, session_id) if state == "input-required" else None
                if answer is None:
                    break
                # The A2A agent asked to use a tool: ask the editor, send the choice back.
                outcome = await self.peer.request("session/request_permission", answer)
                option = (outcome or {}).get("outcome", {})
                chosen = option.get("optionId") if option.get("outcome") == "selected" else "deny"
                message = _user_message("", session.context_id, final_task.get("id"))
                message["parts"] = [{"kind": "data", "data": {"optionId": chosen}}, {"kind": "text", "text": chosen}]
        except AuthError as exc:
            label = self.client.name or "<agent>"
            await self._text(session_id, f"Sign-in needed for this agent: {exc}\nRun `kiro-a2a login {label}`.")
            return {"stopReason": "end_turn"}
        except Exception as exc:
            log.warning("prompt failed: %s", exc)
            await self._text(session_id, f"Could not reach the A2A agent: {exc}")
            return {"stopReason": "end_turn"}
        finally:
            session.task_id = None
        if session.cancelled:
            return {"stopReason": "cancelled"}
        if state == "input-required":
            session.reply_to_task = final_task.get("id")
        return {"stopReason": STOP_FOR_STATE.get(state, "end_turn")}

    async def _run(self, session_id: str, session: _Session, http, message: dict) -> tuple[str, dict]:
        """Send one A2A message and relay what comes back. Returns (state, task)."""
        params = {"message": message, "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain"]}}
        task: dict = {}
        if not self.client.streams:
            reply = await self.client.ask(http, prompt_text_of(message), session.context_id, message.get("taskId"))
            session.context_id = reply.context_id or session.context_id
            await self._text(session_id, reply.text)
            return reply.state, {"id": reply.task_id}

        state = "completed"
        async for event in self.client.events(http, params):
            kind = event.get("kind")
            session.context_id = event.get("contextId") or session.context_id
            if kind == "message":
                await self._text(session_id, parts_text(event.get("parts")))
                break
            if kind == "task":
                task = event
                session.task_id = event.get("id")
                state = (event.get("status") or {}).get("state", state)
                if not message.get("taskId"):  # a resumed task repeats text already shown
                    for artifact in event.get("artifacts") or []:
                        await self._artifact(session_id, artifact)
            elif kind == "artifact-update":
                await self._artifact(session_id, event.get("artifact") or {})
            elif kind == "status-update":
                session.task_id = event.get("taskId") or session.task_id
                status = event.get("status") or {}
                state = status.get("state", state)
                task = {**task, "id": session.task_id, "status": status}
                note = parts_text([p for p in (status.get("message") or {}).get("parts") or [] if "text" in p])
                if state in WAITING_STATES:
                    await self._progress(session_id, event, note)
                elif state != "input-required" or not self._permission_answer(task, session_id):
                    # final words: errors, questions, refusals
                    await self._text(session_id, ("\n\n" + note) if note and state != "completed" else "")
                if event.get("final") or state not in WAITING_STATES:
                    break
        return state, task

    async def _artifact(self, session_id: str, artifact: dict) -> None:
        """File changes become ACP edit cards with a diff; everything else is answer text."""
        for part in artifact.get("parts") or []:
            data = part.get("data") if isinstance(part, dict) else None
            if isinstance(data, dict) and data.get("path") and "newText" in data:
                await self._say(session_id, {
                    "sessionUpdate": "tool_call",
                    "toolCallId": artifact.get("artifactId") or uuid.uuid4().hex,
                    "title": f"Edit {data['path']}",
                    "kind": "edit",
                    "status": "completed",
                    "content": [{"type": "diff", "path": data["path"], "oldText": data.get("oldText"),
                                 "newText": data["newText"]}],
                })
                return
        await self._text(session_id, parts_text(artifact.get("parts")))

    async def _progress(self, session_id: str, event: dict, note: str) -> None:
        meta = event.get("metadata") or {}
        update = meta.get("update") or {}
        if meta.get("kind") == "plan" and update.get("entries"):
            await self._say(session_id, {"sessionUpdate": "plan", "entries": update["entries"]})
        elif note:
            await self._text(session_id, note if meta.get("kind") == "thought" else note + "\n", thought=True)

    def _permission_answer(self, task: dict, session_id: str) -> dict | None:
        """If the task is waiting on a tool permission, build the ACP request for it."""
        for part in ((task.get("status") or {}).get("message") or {}).get("parts") or []:
            data = part.get("data") if isinstance(part, dict) else None
            if isinstance(data, dict) and data.get("type") == "permission_request":
                return {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "a2a-" + str(task.get("id")), **(data.get("toolCall") or {})},
                    "options": data.get("options") or [],
                }
        return None

    async def close(self) -> None:
        if self.http and not self.http.closed:
            await self.http.close()


def prompt_text_of(message: dict) -> str:
    return parts_text(message.get("parts"))


async def serve_acp(client: A2AClient) -> None:
    front = AcpFront(client)

    def ready(peer: Peer) -> None:
        front.peer = peer

    try:
        await run_stdio(front.handle, front.on_notification, ready)
    finally:
        await front.close()
