"""`kiro-a2a serve`: A2A in front, fake ACP agent behind."""

import asyncio

from helpers import rpc, running_bridge, stream, user_message


def test_agent_card():
    async def run():
        async with running_bridge() as (http, base, _):
            async with http.get(base + ".well-known/agent-card.json") as resp:
                card = await resp.json()
        assert card["name"] == "Fake Kiro"
        assert card["url"] == base
        assert card["capabilities"]["streaming"] is True
        assert "security" not in card

    asyncio.run(run())


def test_send_returns_completed_task_and_denies_edits_by_default():
    async def run():
        async with running_bridge() as (http, base, _):
            status, body = await rpc(http, base, "message/send", user_message("hello"))
        assert status == 200
        task = body["result"]
        assert task["kind"] == "task"
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["text"] == "Echo: hello (turn 1, edit denied)"

    asyncio.run(run())


def test_same_context_continues_the_session():
    async def run():
        async with running_bridge() as (http, base, _):
            _, first = await rpc(http, base, "message/send", user_message("one"))
            ctx = first["result"]["contextId"]
            _, second = await rpc(http, base, "message/send", user_message("two", ctx))
            _, other = await rpc(http, base, "message/send", user_message("three"))
        assert "turn 2" in second["result"]["artifacts"][0]["parts"][0]["text"]
        assert second["result"]["contextId"] == ctx
        assert "turn 1" in other["result"]["artifacts"][0]["parts"][0]["text"]

    asyncio.run(run())


def test_allow_all_policy():
    async def run():
        async with running_bridge(permissions="allow-all") as (http, base, _):
            _, body = await rpc(http, base, "message/send", user_message("hi"))
        assert "edit allowed" in body["result"]["artifacts"][0]["parts"][0]["text"]

    asyncio.run(run())


def test_stream_events():
    async def run():
        async with running_bridge() as (http, base, _):
            return await stream(http, base, user_message("hello"))

    events = asyncio.run(run())
    assert events[0]["kind"] == "task"
    kinds = [e.get("metadata", {}).get("kind") for e in events if e["kind"] == "status-update"]
    for expected in ("thought", "plan", "tool_call", "tool_call_update", "permission"):
        assert expected in kinds
    chunks = [e for e in events if e["kind"] == "artifact-update"]
    assert [c["append"] for c in chunks] == [False, True]
    assert "".join(c["artifact"]["parts"][0]["text"] for c in chunks) == "Echo: hello (turn 1, edit denied)"
    assert events[-1]["kind"] == "status-update"
    assert events[-1]["final"] is True
    assert events[-1]["status"]["state"] == "completed"


def test_cancel():
    async def run():
        async with running_bridge() as (http, base, bridge):
            streaming = asyncio.create_task(stream(http, base, user_message("slow please")))
            for _ in range(100):
                running = [r for r in bridge.runs.values() if r.session_id]
                if running:
                    break
                await asyncio.sleep(0.05)
            task_id = running[0].task["id"]
            _, cancelled = await rpc(http, base, "tasks/cancel", {"id": task_id})
            events = await streaming
            _, fetched = await rpc(http, base, "tasks/get", {"id": task_id})
            _, again = await rpc(http, base, "tasks/cancel", {"id": task_id})
        assert cancelled["result"]["status"]["state"] == "canceled"
        assert events[-1]["status"]["state"] == "canceled"
        assert fetched["result"]["status"]["state"] == "canceled"
        assert again["error"]["code"] == -32002

    asyncio.run(run())


def test_recovers_after_agent_crash():
    async def run():
        async with running_bridge() as (http, base, _):
            _, first = await rpc(http, base, "message/send", user_message("before"))
            ctx = first["result"]["contextId"]
            _, crashed = await rpc(http, base, "message/send", user_message("crash now", ctx))
            _, after = await rpc(http, base, "message/send", user_message("after", ctx))
        assert crashed["result"]["status"]["state"] == "failed"
        assert after["result"]["status"]["state"] == "completed"
        # a fresh process means a fresh session
        assert "turn 1" in after["result"]["artifacts"][0]["parts"][0]["text"]

    asyncio.run(run())


def test_errors():
    async def run():
        async with running_bridge() as (http, base, _):
            _, unknown = await rpc(http, base, "nope", {})
            _, empty = await rpc(http, base, "message/send", {"message": {"parts": []}})
            _, missing = await rpc(http, base, "tasks/get", {"id": "missing"})
        assert unknown["error"]["code"] == -32601
        assert empty["error"]["code"] == -32602
        assert missing["error"]["code"] == -32001

    asyncio.run(run())


def test_api_key():
    async def run():
        async with running_bridge(api_key="s3cret") as (http, base, _):
            async with http.get(base + ".well-known/agent-card.json") as resp:
                card = await resp.json()
            no_key, _ = await rpc(http, base, "tasks/get", {"id": "x"})
            wrong, _ = await rpc(http, base, "tasks/get", {"id": "x"}, {"Authorization": "Bearer nope"})
            ok, body = await rpc(http, base, "message/send", user_message("hi"), {"Authorization": "Bearer s3cret"})
            header_ok, _ = await rpc(http, base, "tasks/get", {"id": "x"}, {"X-API-Key": "s3cret"})
        assert card["security"] == [{"bearer": []}]
        assert (no_key, wrong, ok, header_ok) == (401, 401, 200, 200)
        assert body["result"]["status"]["state"] == "completed"

    asyncio.run(run())
