"""Regressions for problems found in end-to-end testing with Kiro."""

import asyncio
import json

import aiohttp
import pytest
from helpers import rpc, running_bridge

from kiro_a2a.a2a_client import A2AClient, parts_text, reply_from_result
from kiro_a2a.auth import AuthConfig, AuthError
from kiro_a2a.kiro_setup import setup_kiro
from kiro_a2a.mcp import McpServer


def test_data_parts_stay_out_of_readable_text():
    question = [{"kind": "text", "text": "Allow the edit?"}, {"kind": "data", "data": {"type": "permission_request"}}]
    assert parts_text(question) == "Allow the edit?"
    assert "permission_request" in parts_text([{"kind": "data", "data": {"type": "permission_request"}}])


def test_answer_comes_before_diffs():
    task = {"kind": "task", "id": "t", "status": {"state": "completed"}, "artifacts": [
        {"artifactId": "d", "name": "diff: a.py", "parts": [{"kind": "text", "text": "DIFF"}]},
        {"artifactId": "r", "name": "response", "parts": [{"kind": "text", "text": "ANSWER"}]},
    ]}
    assert reply_from_result(task).text == "ANSWER\n\nDIFF"


def test_unknown_task_id_is_rejected_not_started():
    async def run():
        async with running_bridge() as (http, url, bridge):
            _, body = await rpc(http, url, "message/send", {"message": {
                "kind": "message", "role": "user", "messageId": "m", "taskId": "nope",
                "parts": [{"kind": "text", "text": "allow"}]}})
            return body, len(bridge.store.list())

    body, stored = asyncio.run(run())
    assert body["error"]["code"] == -32001
    assert stored == 0


def test_late_answer_returns_the_finished_result():
    async def run():
        async with running_bridge(permissions="ask", permission_timeout=0.2) as (_, url, _):
            server = McpServer([A2AClient(url, name="agent")])
            first = await server.handle("tools/call", {"name": "ask_agent", "arguments": {"message": "fix it"}})
            ids = dict(part.split(": ") for part in first["content"][0]["text"].rsplit("---\n", 1)[1].split(" | "))
            await asyncio.sleep(1)  # the question times out and the task finishes
            late = await server.handle("tools/call", {"name": "ask_agent", "arguments": {
                "message": "allow", "context_id": ids["context_id"], "task_id": ids["task_id"]}})
            await server.close()
        return first, late

    first, late = asyncio.run(run())
    assert "state: input-required" in first["content"][0]["text"]
    text = late["content"][0]["text"]
    assert late["isError"] is False
    assert "after the question had closed" in text and "edit denied" in text and "state: completed" in text


def test_unset_secret_is_named(monkeypatch):
    monkeypatch.delenv("MISSING_SECRET", raising=False)

    async def run():
        async with running_bridge() as (_, url, _):
            client = A2AClient(url, auth=AuthConfig.from_dict(
                {"type": "oauth2", "client_id": "x", "client_secret": "${MISSING_SECRET}", "token_url": "http://127.0.0.1:1/t"}))
            async with aiohttp.ClientSession() as http:
                await client.fetch_card(http)
                await client.auth.resolve(http, client.card)

    with pytest.raises(AuthError, match="MISSING_SECRET"):
        asyncio.run(run())


def test_sign_in_hint_only_when_a_person_signs_in(tmp_path, monkeypatch):
    monkeypatch.delenv("SVC_SECRET", raising=False)
    people = setup_kiro("people", "http://127.0.0.1:1", str(tmp_path), auth={"type": "oauth2", "client_id": "app"})
    service = setup_kiro("svc", "http://127.0.0.1:1", str(tmp_path),
                         auth={"type": "oauth2", "client_id": "bot", "client_secret": "${SVC_SECRET}"})
    assert any("kiro-a2a login people" in line for line in people)
    assert not any("login" in line for line in service)
    assert any("SVC_SECRET" in line for line in service)
    config = json.loads((tmp_path / ".kiro" / "a2a-agents.json").read_text())
    assert config["agents"][1]["auth"]["client_secret"] == "${SVC_SECRET}"  # never the value
