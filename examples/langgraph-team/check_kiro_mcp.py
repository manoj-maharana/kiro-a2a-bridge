"""End-to-end check of what Kiro (or Amazon Q) will see, without opening the IDE.

Reads the MCP config the IDE uses, starts each server exactly the way the IDE
does, and speaks MCP to it: initialize, tools/list, then tool calls with a
progress token. It also checks a follow-up (context_id) and an agent question
(input-required) on the orchestrator.

    python check_kiro_mcp.py                       # ~/.kiro/settings/mcp.json
    python check_kiro_mcp.py path/to/mcp.json
"""

import asyncio
import json
import os
import re
import sys

CONFIG = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.kiro/settings/mcp.json")


class McpSession:
    def __init__(self, name, spec):
        self.name, self.spec, self.next_id = name, spec, 0

    async def __aenter__(self):
        self.proc = await asyncio.create_subprocess_exec(
            self.spec["command"], *self.spec.get("args", []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **self.spec.get("env", {})},
        )
        await self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "check", "version": "1"}})
        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        return self

    async def __aexit__(self, *exc):
        self.proc.stdin.close()
        await asyncio.wait_for(self.proc.wait(), 10)

    async def call(self, method, params, progress=None):
        self.next_id += 1
        if progress is not None:
            params = {**params, "_meta": {"progressToken": f"p{self.next_id}"}}
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()
        while True:
            reply = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 120))
            if reply.get("method") == "notifications/progress" and progress is not None:
                progress.append(reply["params"]["message"])
            elif reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]

    async def ask(self, tool, **arguments):
        progress = []
        result = await self.call("tools/call", {"name": tool, "arguments": arguments}, progress)
        text = result["content"][0]["text"]
        footer = dict(re.findall(r"(context_id|task_id|state): ([^ |\n]+)", text))
        return text, footer, progress, result["isError"]


def show(label, text, progress):
    print(f"\n  > {label}")
    for line in progress:
        print(f"    progress: {line}")
    print("    " + text.replace("\n", "\n    "))


async def main(servers):
    failures = 0
    for name, spec in servers.items():
        if not any(key in name for key in ("orchestrator", "calc", "text")):
            print(f"\n=== MCP server '{name}': not part of this example, skipped")
            continue
        print(f"\n=== MCP server '{name}'")
        async with McpSession(name, spec) as mcp:
            tools = (await mcp.call("tools/list", {}))["tools"]
            print(f"  tools: {[t['name'] for t in tools]}")
            tool = tools[0]["name"]
            if "orchestrator" in name:
                text, footer, progress, err = await mcp.ask(tool, message="calculate (12 + 30) * 2 and then uppercase: hello team")
                show("parent delegates to both sub-agents", text, progress)
                failures += err or "= 84" not in text or "HELLO TEAM" not in text
                text, _, progress, err = await mcp.ask(tool, message="what was the last result?", context_id=footer["context_id"])
                show("follow-up in the same conversation", text, progress)
                failures += err or "HELLO TEAM" not in text
                text, footer, progress, err = await mcp.ask(tool, message="calculate the sum", context_id=footer["context_id"])
                show("agent asks a question (input-required)", text, progress)
                failures += footer.get("state") != "input-required"
                text, _, progress, err = await mcp.ask(tool, message="19 + 23", context_id=footer["context_id"],
                                                       task_id=footer["task_id"])
                show("answer the question on the same task", text, progress)
                failures += err or "= 42" not in text
            elif "calc" in name:
                text, _, progress, err = await mcp.ask(tool, message="sqrt(144) + 2 ** 3")
                show("direct call to the sub-agent", text, progress)
                failures += err or "= 20" not in text
            else:
                text, _, progress, err = await mcp.ask(tool, message="count words: the quick brown fox")
                show("direct call to the sub-agent", text, progress)
                failures += err or "4 words" not in text
    print("\nALL CHECKS PASSED" if not failures else f"\n{failures} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    with open(CONFIG, encoding="utf-8") as fh:
        sys.exit(asyncio.run(main(json.load(fh)["mcpServers"])))
