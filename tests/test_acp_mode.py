"""`kiro-a2a acp`: an ACP editor talking to an A2A agent through the bridge."""

import asyncio
import json
import os
import sys

from helpers import running_bridge

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class Editor:
    """Plays the ACP client (the editor) over the bridge's stdin/stdout."""

    def __init__(self, proc, allow=True):
        self.proc = proc
        self.allow = allow
        self.next_id = 0
        self.updates = []
        self.permission_requests = []

    async def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method, params):
        self.next_id += 1
        my_id = self.next_id
        await self.send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
        while True:
            msg = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 30))
            if msg.get("method") == "session/update":
                self.updates.append(msg["params"]["update"])
            elif msg.get("method") == "session/request_permission":
                self.permission_requests.append(msg["params"])
                wanted = "allow_once" if self.allow else "reject_once"
                option = next(o for o in msg["params"]["options"] if o["kind"] == wanted)
                await self.send({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}})
            elif msg.get("id") == my_id:
                return msg

    def said(self, kind="agent_message_chunk"):
        return "".join(u["content"]["text"] for u in self.updates if u["sessionUpdate"] == kind)


async def start_acp(url):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "kiro_a2a", "acp", "--agent", f"helper={url}",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": SRC},
    )


def test_acp_editor_conversation():
    async def run():
        async with running_bridge() as (_, url, _):
            proc = await start_acp(url)
            editor = Editor(proc)
            init = await editor.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            session = (await editor.request("session/new", {"cwd": ".", "mcpServers": []}))["result"]["sessionId"]
            first = await editor.request("session/prompt", {"sessionId": session, "prompt": [
                {"type": "text", "text": "review"},
                {"type": "resource", "resource": {"uri": "file:///a.py", "text": "print(1)"}},
            ]})
            said_first, thoughts, plans = editor.said(), editor.said("agent_thought_chunk"), \
                [u for u in editor.updates if u["sessionUpdate"] == "plan"]
            editor.updates.clear()
            second = await editor.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "more"}]})
            said_second = editor.said()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 10)
        return init, first, second, said_first, said_second, thoughts, plans

    init, first, second, said_first, said_second, thoughts, plans = asyncio.run(run())
    assert init["result"]["agentInfo"]["name"] == "Fake Kiro"
    assert first["result"]["stopReason"] == "end_turn"
    assert said_first.startswith("Echo: review") and "File file:///a.py" in said_first
    assert "(turn 1, edit denied)" in said_first
    assert "thinking about it" in thoughts and "Read README.md" in thoughts
    assert plans and plans[0]["entries"][0]["content"] == "read files"
    assert said_second == "Echo: more (turn 2, edit denied)"


def test_acp_editor_answers_permission_questions():
    async def run():
        async with running_bridge(permissions="ask") as (_, url, _):
            proc = await start_acp(url)
            editor = Editor(proc, allow=True)
            await editor.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            session = (await editor.request("session/new", {"cwd": ".", "mcpServers": []}))["result"]["sessionId"]
            result = await editor.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "edit it"}]})
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 10)
        return result, editor

    result, editor = asyncio.run(run())
    assert result["result"]["stopReason"] == "end_turn"
    assert editor.permission_requests[0]["toolCall"]["title"] == "Edit main.py"
    assert editor.said() == "Echo: edit it (turn 1, edit allowed)"


def test_acp_editor_gets_edit_cards_for_diffs():
    async def run():
        async with running_bridge(permissions="allow-all") as (_, url, _):
            proc = await start_acp(url)
            editor = Editor(proc)
            await editor.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            session = (await editor.request("session/new", {"cwd": ".", "mcpServers": []}))["result"]["sessionId"]
            await editor.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "fix"}]})
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 10)
        return editor

    editor = asyncio.run(run())
    edits = [u for u in editor.updates if u["sessionUpdate"] == "tool_call" and u.get("kind") == "edit"]
    assert edits[0]["content"][0] == {"type": "diff", "path": "main.py", "oldText": "print(1)\n", "newText": "print(2)\n"}
    assert editor.said() == "Echo: fix (turn 1, edit allowed)"
