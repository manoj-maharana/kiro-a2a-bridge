"""A2A 1.0, attachments, diffs, usage, task listing, resubscribe, push, SQLite, signed cards."""

import asyncio
import base64
import json

import aiohttp
from aiohttp import web
from helpers import rpc, running_bridge, user_message

from kiro_a2a.a2a_client import A2AClient
from kiro_a2a.card_signing import CardSigner, verify_card
from kiro_a2a.protocol import card_to_v1, result_from_v1, result_to_v1
from kiro_a2a.push import PushNotifier
from kiro_a2a.store import SqliteTaskStore


def v1_message(text, **extra):
    return {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}], **extra}}


def test_a2a_1_0_methods():
    async def run():
        async with running_bridge() as (http, url, _):
            _, sent = await rpc(http, url, "SendMessage", v1_message("hello"))
            task_id = sent["result"]["task"]["id"]
            _, got = await rpc(http, url, "GetTask", {"id": task_id})
            _, listed = await rpc(http, url, "ListTasks", {"contextId": sent["result"]["task"]["contextId"]})
            body = {"jsonrpc": "2.0", "id": 3, "method": "SendStreamingMessage", "params": v1_message("again")}
            events = []
            async with http.post(url, json=body) as resp:
                async for line in resp.content:
                    if line.startswith(b"data: "):
                        events.append(json.loads(line[6:])["result"])
            async with http.get(url + ".well-known/agent-card.json", headers={"A2A-Version": "1.0"}) as resp:
                card = await resp.json()
        return sent["result"], got["result"], listed["result"], events, card

    sent, got, listed, events, card = asyncio.run(run())
    task = sent["task"]
    assert "kind" not in task and task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"] == [{"text": "Echo: hello (turn 1, edit denied)"}]
    assert task["history"][0]["role"] == "ROLE_USER"
    assert got["status"]["state"] == "TASK_STATE_COMPLETED"
    assert [t["id"] for t in listed["tasks"]] == [task["id"]] and listed["nextPageToken"] == ""
    assert set(events[0]) == {"task"} and set(events[-1]) == {"statusUpdate"}
    assert events[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert any("artifactUpdate" in e for e in events)
    assert "url" not in card and card["supportedInterfaces"][0]["protocolVersion"] == "1.0"


def test_client_speaks_1_0_when_the_card_says_so():
    async def run():
        async with running_bridge() as (http, url, bridge):
            client = A2AClient(url)
            client._use_card({**card_to_v1(bridge.agent_card(), url)})
            assert client.wire == "1.0"
            async with aiohttp.ClientSession() as session:
                return await client.ask(session, "over 1.0", on_progress=lambda t, k=None: asyncio.sleep(0))

    reply = asyncio.run(run())
    assert reply.state == "completed" and reply.text.startswith("Echo: over 1.0")


def test_round_trip_conversions():
    task = {"kind": "task", "id": "t", "contextId": "c", "status": {"state": "input-required"},
            "artifacts": [{"artifactId": "a", "parts": [
                {"kind": "file", "file": {"bytes": "eA==", "mimeType": "text/plain", "name": "x.txt"}}]}],
            "history": [{"kind": "message", "role": "user", "messageId": "m", "parts": [{"kind": "data", "data": {"a": 1}}]}]}
    v1 = result_to_v1(task)
    assert v1["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert v1["task"]["artifacts"][0]["parts"][0] == {"raw": "eA==", "mediaType": "text/plain", "filename": "x.txt"}
    assert result_from_v1(v1) == task
    event = {"kind": "status-update", "taskId": "t", "contextId": "c", "status": {"state": "completed"}, "final": True}
    assert result_from_v1(result_to_v1(event)) == event


def test_attachments_diffs_and_usage(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("remember the milk")
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG fake")

    async def run():
        async with running_bridge(permissions="allow-all") as (_, url, _):
            async with aiohttp.ClientSession() as http:
                client = A2AClient(url)
                reply = await client.ask(http, "look", files=[str(notes), str(image)])
                task = await client._rpc(http, "tasks/get", {"id": reply.task_id})
        return reply, task

    reply, task = asyncio.run(run())
    assert "Echo: look [resource attachment:///notes.txt] [image image/png]" in reply.text
    diff = next(a for a in task["artifacts"] if a["name"] == "diff: main.py")
    assert "-print(1)\n+print(2)\n" in diff["parts"][0]["text"]
    assert diff["parts"][1]["data"]["newText"] == "print(2)\n"
    assert task["metadata"]["toolCalls"] == 2 and task["metadata"]["durationMs"] >= 0
    assert task["metadata"]["stopReason"] == "end_turn"


def test_resubscribe_and_stale_answers():
    async def run():
        async with running_bridge(permissions="ask") as (http, url, bridge):
            _, asked = await rpc(http, url, "message/send", user_message("hi"))
            task = asked["result"]
            request_id = task["status"]["message"]["parts"][1]["data"]["requestId"]
            stale = {"message": {"kind": "message", "role": "user", "messageId": "x", "taskId": task["id"],
                                 "contextId": task["contextId"],
                                 "parts": [{"kind": "data", "data": {"requestId": "old", "optionId": "yes"}}]}}
            _, refused = await rpc(http, url, "message/send", stale)
            stale["message"]["parts"][0]["data"]["requestId"] = request_id
            watcher = asyncio.create_task(stream_resubscribe(http, url, task["id"]))
            await asyncio.sleep(0.2)
            _, done = await rpc(http, url, "message/send", stale)
            finished = await stream_resubscribe(http, url, task["id"])
            watcher.cancel()
        return refused, done["result"], finished

    refused, done, finished = asyncio.run(run())
    assert refused["error"]["code"] == -32602 and "no longer open" in refused["error"]["message"]
    assert done["status"]["state"] == "completed" and "edit allowed" in done["artifacts"][0]["parts"][0]["text"]
    assert finished[-1]["status"]["state"] == "completed"  # resubscribing to a finished task returns it


async def stream_resubscribe(http, url, task_id):
    body = {"jsonrpc": "2.0", "id": 9, "method": "tasks/resubscribe", "params": {"id": task_id}}
    events = []
    async with http.post(url, json=body) as resp:
        async for line in resp.content:
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:])["result"])
    return events


def test_push_notifications():
    async def run():
        received = []

        async def hook(request):
            received.append((request.headers.get("X-A2A-Notification-Token"), await request.json()))
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/hook", hook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        hook_url = f"http://127.0.0.1:{runner.addresses[0][1]}/hook"
        try:
            async with running_bridge() as (http, url, _):
                _, off = await rpc(http, url, "message/send", {**user_message("x"), "configuration": {
                    "pushNotificationConfig": {"url": hook_url}}})
            async with running_bridge(push=PushNotifier(True, ["127.0.0.1"])) as (http, url, bridge):
                _, blocked = await rpc(http, url, "message/send", {**user_message("x"), "configuration": {
                    "pushNotificationConfig": {"url": "http://elsewhere.example.com/hook"}}})
                _, sent = await rpc(http, url, "message/send", {**user_message("x"), "configuration": {
                    "pushNotificationConfig": {"url": hook_url, "token": "t0k"}}})
                await asyncio.sleep(0.3)
                _, listed = await rpc(http, url, "tasks/pushNotificationConfig/list", {"id": sent["result"]["id"]})
        finally:
            await runner.cleanup()
        return off, blocked, received, listed["result"]

    off, blocked, received, listed = asyncio.run(run())
    assert off["error"]["code"] == -32003
    assert blocked["error"]["code"] == -32602
    states = [body["status"]["state"] for _, body in received]
    assert states == ["working", "completed"] and all(token == "t0k" for token, _ in received)
    assert listed[0]["pushNotificationConfig"]["url"].endswith("/hook")


def test_sqlite_task_store(tmp_path):
    db = str(tmp_path / "tasks.db")

    async def run():
        async with running_bridge(store=SqliteTaskStore(db)) as (http, url, _):
            _, sent = await rpc(http, url, "message/send", user_message("keep me"))
        async with running_bridge(store=SqliteTaskStore(db)) as (http, url, _):
            _, got = await rpc(http, url, "tasks/get", {"id": sent["result"]["id"]})
        return got["result"]

    got = asyncio.run(run())
    assert got["status"]["state"] == "completed" and "keep me" in got["artifacts"][0]["parts"][0]["text"]

    store = SqliteTaskStore(db)
    store.put({"id": "live", "contextId": "c", "status": {"state": "working"}})
    store.close()
    assert SqliteTaskStore(db).get("live")["status"]["state"] == "failed"  # was running at "restart"


def test_signed_agent_cards():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo)
    other_pem = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    async def run(tmp_key, wrong_key):
        async with running_bridge(signer=CardSigner(private_pem, "k1", "ES256")) as (_, url, bridge):
            async with aiohttp.ClientSession() as http:
                good = A2AClient(url, card_key=tmp_key)
                await good.fetch_card(http)
                bad = A2AClient(url, card_key=wrong_key)
                try:
                    await bad.fetch_card(http)
                    refused = None
                except Exception as exc:
                    refused = str(exc)
        return good.card, refused

    import os
    import tempfile  # noqa: E401
    with tempfile.TemporaryDirectory() as d:
        good_path, bad_path = os.path.join(d, "pub.pem"), os.path.join(d, "other.pem")
        open(good_path, "wb").write(public_pem)
        open(bad_path, "wb").write(other_pem)
        card, refused = asyncio.run(run(good_path, bad_path))
    assert verify_card(card, public_pem) == "k1"
    assert "does not match" in refused
    tampered = {**card, "name": "Someone else"}
    try:
        verify_card(tampered, public_pem)
        raise AssertionError("tampered card verified")
    except ValueError:
        pass
    hs = CardSigner(b"shared-secret", "h1", "HS256").sign({"name": "x"})
    assert verify_card(hs, b"shared-secret") == "h1"


def test_demo_preset_and_image_capability():
    from kiro_a2a.acp import AcpAgent
    from kiro_a2a.presets import preset

    async def run():
        agent = AcpAgent(preset("demo")["command"])
        try:
            await agent.ensure_started()
            session, _ = await agent.new_session()
            seen = []
            image = base64.b64encode(b"img").decode()
            result = await agent.prompt(session, [{"type": "text", "text": "hi"},
                                                  {"type": "image", "mimeType": "image/png", "data": image}], seen.append)
            return agent.prompt_capabilities, result, seen
        finally:
            await agent.close()

    caps, result, seen = asyncio.run(run())
    assert caps["image"] is True and result["stopReason"] == "end_turn"
    text = "".join(u["content"]["text"] for u in seen if u["sessionUpdate"] == "agent_message_chunk")
    assert text.startswith("Echo: hi [image image/png]")


def test_attachment_type_detection():
    from kiro_a2a.attachments import to_prompt_blocks

    def part(name, mime, raw):
        return {"kind": "file", "file": {"name": name, "mimeType": mime, "bytes": base64.b64encode(raw).decode()}}

    blocks = to_prompt_blocks({"parts": [
        part("notes.md", "application/octet-stream", b"# Title\n"),
        part("blob.bin", "application/octet-stream", b"\x00\x01\x02"),
        part("big.txt", "text/plain", b"x" * (600 * 1024)),
    ]}, {"embeddedContext": True})
    assert blocks[0]["resource"]["text"] == "# Title\n"
    assert "not sent" in blocks[1]["text"]
    assert blocks[2]["resource"]["text"].endswith("more characters not sent]")
