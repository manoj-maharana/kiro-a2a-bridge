"""Kiro naming variants, session reload, timeouts, parallel limit, and `ask` permissions."""

import asyncio

from helpers import rpc, running_bridge, stream, user_message

from kiro_a2a.acp import normalize_stop_reason, normalize_update
from kiro_a2a.server import choose_permission


def answer_text(task):
    return task["artifacts"][0]["parts"][0]["text"]


def reply_on(task, *parts):
    return {"message": {"kind": "message", "role": "user", "messageId": "answer",
                        "taskId": task["id"], "contextId": task["contextId"], "parts": list(parts)}}


def test_name_normalizing():
    assert normalize_update({"sessionUpdate": "AgentMessageChunk"})["sessionUpdate"] == "agent_message_chunk"
    assert normalize_update({"type": "ToolCallUpdate"})["sessionUpdate"] == "tool_call_update"
    assert normalize_update({"sessionUpdate": "plan"}) == {"sessionUpdate": "plan"}
    assert normalize_stop_reason("EndTurn") == "end_turn"
    assert normalize_stop_reason("TurnEnd") == "end_turn"
    assert normalize_stop_reason("Canceled") == "cancelled"
    assert normalize_stop_reason(None) == "end_turn"


def test_pascal_case_agent():
    async def run():
        async with running_bridge(env={"FAKE_STYLE": "pascal"}) as (http, base, _):
            _, body = await rpc(http, base, "message/send", user_message("hello"))
            events = await stream(http, base, user_message("again"))
        return body["result"], events

    task, events = asyncio.run(run())
    assert task["status"]["state"] == "completed"
    assert answer_text(task) == "Echo: hello (turn 1, edit denied)"
    kinds = {e.get("metadata", {}).get("kind") for e in events}
    assert {"thought", "plan", "tool_call"} <= kinds


def test_session_is_reloaded_after_crash(tmp_path):
    async def run():
        env = {"FAKE_STATE_FILE": str(tmp_path / "state.json")}
        async with running_bridge(env=env) as (http, base, _):
            _, first = await rpc(http, base, "message/send", user_message("before"))
            ctx = first["result"]["contextId"]
            _, crashed = await rpc(http, base, "message/send", user_message("crash", ctx))
            _, after = await rpc(http, base, "message/send", user_message("after", ctx))
        return crashed["result"], after["result"]

    crashed, after = asyncio.run(run())
    assert crashed["status"]["state"] == "failed"
    assert after["status"]["state"] == "completed"
    assert answer_text(after) == "Echo: after (turn 2, edit denied)"  # same session, replay not leaked


def test_prompt_timeout():
    async def run():
        async with running_bridge(prompt_timeout=0.5) as (http, base, _):
            _, slow = await rpc(http, base, "message/send", user_message("slow"))
            _, fine = await rpc(http, base, "message/send", user_message("quick"))
        return slow["result"], fine["result"]

    slow, fine = asyncio.run(run())
    assert slow["status"]["state"] == "failed"
    assert "did not finish within 0.5s" in slow["status"]["message"]["parts"][0]["text"]
    assert fine["status"]["state"] == "completed"


def test_parallel_limit():
    async def run():
        async with running_bridge(max_parallel=1) as (http, base, _):
            return await asyncio.gather(*(rpc(http, base, "message/send", user_message(f"m{i}")) for i in range(3)))

    results = asyncio.run(run())
    assert [r[1]["result"]["status"]["state"] for r in results] == ["completed"] * 3


def test_choose_permission():
    options = [
        {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
        {"optionId": "forever", "name": "Always allow", "kind": "allow_always"},
        {"optionId": "no", "name": "Reject", "kind": "reject_once"},
    ]

    def pick(*parts):
        return choose_permission(options, {"parts": list(parts)}).get("optionId")

    assert pick({"kind": "text", "text": "allow"}) == "yes"
    assert pick({"kind": "text", "text": "yes, always"}) == "forever"
    assert pick({"kind": "text", "text": "Reject"}) == "no"
    assert pick({"kind": "text", "text": "no, don't allow"}) == "no"
    assert pick({"kind": "text", "text": "hmm?"}) == "no"
    assert pick({"kind": "data", "data": {"optionId": "forever"}}) == "forever"


def test_ask_mode_send_round_trip():
    async def run():
        async with running_bridge(permissions="ask") as (http, base, _):
            _, asked = await rpc(http, base, "message/send", user_message("hi"))
            task = asked["result"]
            _, done = await rpc(http, base, "message/send", reply_on(task, {"kind": "text", "text": "allow"}))
            _, asked2 = await rpc(http, base, "message/send", user_message("hi again", task["contextId"]))
            _, denied = await rpc(http, base, "message/send",
                                  reply_on(asked2["result"], {"kind": "data", "data": {"optionId": "no"}}))
            _, late = await rpc(http, base, "message/send", reply_on(task, {"kind": "text", "text": "allow"}))
        return task, done["result"], denied["result"], late

    task, done, denied, late = asyncio.run(run())
    assert task["status"]["state"] == "input-required"
    parts = task["status"]["message"]["parts"]
    assert "Edit main.py" in parts[0]["text"] and task["id"] in parts[0]["text"]
    assert parts[1]["data"]["type"] == "permission_request"
    assert [o["optionId"] for o in parts[1]["data"]["options"]] == ["yes", "no"]
    assert done["id"] == task["id"] and done["status"]["state"] == "completed"
    assert answer_text(done) == "Echo: hi (turn 1, edit allowed)"
    assert answer_text(denied) == "Echo: hi again (turn 2, edit denied)"
    assert late["error"]["code"] == -32602  # that task is already finished


def test_ask_mode_stream_round_trip():
    async def run():
        async with running_bridge(permissions="ask") as (http, base, _):
            first = await stream(http, base, user_message("hi"))
            second = await stream(http, base, reply_on(first[-1] | {"id": first[-1]["taskId"]},
                                                      {"kind": "text", "text": "yes"}))
        return first, second

    first, second = asyncio.run(run())
    assert first[-1]["final"] is True and first[-1]["status"]["state"] == "input-required"
    assert second[-1]["status"]["state"] == "completed"
    chunks = "".join(e["artifact"]["parts"][0]["text"] for e in second
                     if e["kind"] == "artifact-update" and e["artifact"]["name"] == "response")
    assert chunks == "Echo: hi (turn 1, edit allowed)"


def test_ask_mode_timeout_and_cancel():
    async def run():
        async with running_bridge(permissions="ask", permission_timeout=0.3) as (http, base, _):
            _, asked = await rpc(http, base, "message/send", user_message("hi"))
            for _ in range(50):
                _, got = await rpc(http, base, "tasks/get", {"id": asked["result"]["id"]})
                if got["result"]["status"]["state"] == "completed":
                    break
                await asyncio.sleep(0.1)
        async with running_bridge(permissions="ask") as (http, base, _):
            _, waiting = await rpc(http, base, "message/send", user_message("slow"))
            _, cancelled = await rpc(http, base, "tasks/cancel", {"id": waiting["result"]["id"]})
        return got["result"], cancelled["result"]

    timed_out, cancelled = asyncio.run(run())
    assert timed_out["status"]["state"] == "completed"
    assert answer_text(timed_out) == "Echo: hi (turn 1, edit denied)"
    assert cancelled["status"]["state"] == "canceled"


def test_kiro_sign_in_methods():
    async def run():
        async with running_bridge(env={"FAKE_AUTH": "org-profile"}) as (http, base, _):
            _, single = await rpc(http, base, "message/send", user_message("hi"))
        async with running_bridge(env={"FAKE_AUTH": "profile-a,profile-b"}) as (http, base, _):
            _, several = await rpc(http, base, "message/send", user_message("hi"))
        async with running_bridge(env={"FAKE_AUTH": "profile-a,profile-b"}) as (http, base, bridge):
            bridge.agent.auth_method = "profile-b"
            _, chosen = await rpc(http, base, "message/send", user_message("hi"))
        return single["result"], several["result"], chosen["result"]

    single, several, chosen = asyncio.run(run())
    assert single["status"]["state"] == "completed"  # the only method is used automatically
    assert several["status"]["state"] == "failed"
    assert "--kiro-auth-method: profile-a" in several["status"]["message"]["parts"][0]["text"]
    assert chosen["status"]["state"] == "completed"
