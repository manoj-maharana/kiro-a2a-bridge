"""Live progress, agent questions (input-required) and `setup-kiro`."""

import asyncio
import json

from aiohttp import web
from helpers import running_bridge

from kiro_a2a.a2a_client import A2AClient
from kiro_a2a.kiro_setup import setup_kiro
from kiro_a2a.mcp import McpServer


def test_progress_is_streamed_to_kiro():
    async def run():
        async with running_bridge() as (_, url, _):
            sent = []

            async def notify(method, params):
                sent.append((method, params))

            server = McpServer([A2AClient(url, name="helper")], notify=notify)
            result = await server.handle(
                "tools/call",
                {"name": "ask_helper", "arguments": {"message": "hi"}, "_meta": {"progressToken": "tok"}},
            )
            await server.close()
        return sent, result

    sent, result = asyncio.run(run())
    assert sent and all(m == "notifications/progress" and p["progressToken"] == "tok" for m, p in sent)
    messages = [p["message"] for _, p in sent]
    assert messages[0] == "Thinking: thinking about it"
    assert any("Read README.md" in m for m in messages)
    assert any(m.startswith("Denied: Edit main.py") for m in messages)
    assert [p["progress"] for _, p in sent] == list(range(1, len(sent) + 1))
    assert "Echo: hi (turn 1, edit denied)" in result["content"][0]["text"]


def test_no_progress_token_means_plain_send():
    async def run():
        async with running_bridge() as (_, url, _):
            sent = []

            async def notify(method, params):
                sent.append(method)

            server = McpServer([A2AClient(url, name="helper")], notify=notify)
            result = await server.handle("tools/call", {"name": "ask_helper", "arguments": {"message": "hi"}})
            await server.close()
        return sent, result

    sent, result = asyncio.run(run())
    assert sent == []
    assert "Echo: hi" in result["content"][0]["text"]


async def question_agent():
    """Asks for a date first, then finishes once the answer comes back on the same task."""

    async def card(request):
        return web.json_response({"name": "Booking", "url": str(request.url.origin()) + "/"})

    async def rpc(request):
        body = await request.json()
        msg = body["params"]["message"]
        text = msg["parts"][0]["text"]
        if msg.get("taskId") == "task-1":
            task = {"kind": "task", "id": "task-1", "contextId": "ctx-b", "status": {"state": "completed"},
                    "artifacts": [{"artifactId": "a", "parts": [{"kind": "text", "text": f"Booked for {text}"}]}]}
        else:
            task = {"kind": "task", "id": "task-1", "contextId": "ctx-b",
                    "status": {"state": "input-required",
                               "message": {"kind": "message", "role": "agent", "messageId": "q",
                                           "parts": [{"kind": "text", "text": "Which date?"}]}}}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": task})

    app = web.Application()
    app.router.add_get("/.well-known/agent-card.json", card)
    app.router.add_post("/", rpc)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


def test_agent_question_round_trip():
    async def run():
        runner, url = await question_agent()
        try:
            server = McpServer([A2AClient(url)])
            asked = await server.handle("tools/call", {"name": "ask_booking", "arguments": {"message": "book a room"}})
            answered = await server.handle(
                "tools/call",
                {"name": "ask_booking", "arguments": {"message": "May 3", "context_id": "ctx-b", "task_id": "task-1"}},
            )
            await server.close()
        finally:
            await runner.cleanup()
        return asked, answered

    asked, answered = asyncio.run(run())
    text = asked["content"][0]["text"]
    assert "needs more information" in text and "Which date?" in text
    assert "task_id=task-1" in text and "state: input-required" in text
    assert asked["isError"] is False
    assert answered["content"][0]["text"].startswith("Booked for May 3")


def test_setup_kiro(tmp_path):
    settings = tmp_path / ".kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    async def run():
        async with running_bridge() as (_, url, _):
            # asyncio.run inside setup_kiro needs its own thread
            return url, await asyncio.to_thread(
                setup_kiro, "My Helper", url, str(tmp_path), ["Authorization: Bearer ${AGENT_TOKEN}"]
            )

    url, report = asyncio.run(run())
    mcp = json.loads((settings / "mcp.json").read_text())
    assert set(mcp["mcpServers"]) == {"other", "my_helper"}
    args = mcp["mcpServers"]["my_helper"]["args"]
    config_path = str(tmp_path / ".kiro" / "a2a-agents.json")
    assert args[args.index("--config") + 1] == config_path
    assert args[args.index("--only") + 1] == "my_helper"
    agents = json.loads((tmp_path / ".kiro" / "a2a-agents.json").read_text())["agents"]
    assert agents == [{"name": "my_helper", "url": url,
                       "headers": {"Authorization": "Bearer ${AGENT_TOKEN}"}}]  # secret stays a placeholder

    agent = json.loads((tmp_path / ".kiro" / "agents" / "my_helper.json").read_text())
    assert agent["name"] == "my_helper"
    assert agent["tools"] == ["@my_helper"] and agent["allowedTools"] == ["@my_helper"]
    assert agent["resources"] == ["file://.kiro/steering/my_helper.md"]
    assert "Kiro coding agent" in agent["description"]  # read from the agent card

    steering = (tmp_path / ".kiro" / "steering" / "my_helper.md").read_text()
    assert "inclusion: always" in steering and "ask_my_helper" in steering

    again = setup_kiro("My Helper", "http://127.0.0.1:1", str(tmp_path))
    assert any(line.startswith("kept existing") for line in again)
    assert "Kiro coding agent" in (tmp_path / ".kiro" / "agents" / "my_helper.json").read_text()


def test_add_list_remove(tmp_path):
    from kiro_a2a.kiro_setup import list_agents, remove_agent

    setup_kiro("Reviewer", "http://127.0.0.1:1", str(tmp_path), amazon_q=True)
    rows = list_agents(str(tmp_path))
    assert [(r["name"], r["tool"], r["in_kiro"], r["custom_agent"]) for r in rows] == [("reviewer", "ask_reviewer", True, True)]
    report = remove_agent("Reviewer", str(tmp_path))
    assert len(report) == 5
    assert list_agents(str(tmp_path)) == []
    assert json.loads((tmp_path / ".kiro" / "settings" / "mcp.json").read_text())["mcpServers"] == {}
    assert json.loads((tmp_path / ".amazonq" / "mcp.json").read_text())["mcpServers"] == {}
    assert remove_agent("reviewer", str(tmp_path))[0].startswith("nothing to remove")
