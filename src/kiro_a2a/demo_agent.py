"""A small ACP agent for trying the bridge without Kiro (and for the tests).

    kiro-a2a serve --agent demo

It needs no account or network. For each prompt it thinks, shows a plan,
"reads" a file, asks permission to edit main.py (and, if allowed, reports
the edit as a diff), then answers "Echo: <text> (turn N, edit allowed|denied)".
  - "slow" in the text: waits up to 5s, so a cancel can arrive
  - "crash" in the text: the process exits mid-turn

Environment switches:
  FAKE_STYLE=pascal   send PascalCase update names over session/notification
  FAKE_STATE_FILE=f   support session/load, keeping turn counts in file f
  FAKE_AUTH=a,b       offer these sign-in methods; session/new fails until one is used
"""

import itertools
import json
import os
import sys
import threading
import time

out_lock = threading.Lock()
pending: dict = {}
cancelled: set = set()
turns: dict = {}
request_ids = itertools.count(1000)
session_ids = itertools.count(1)
PASCAL = os.environ.get("FAKE_STYLE") == "pascal"
STATE_FILE = os.environ.get("FAKE_STATE_FILE")
AUTH_METHODS = [m for m in os.environ.get("FAKE_AUTH", "").split(",") if m]
signed_in = not AUTH_METHODS


def send(msg):
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def ask_client(method, params):
    msg_id = next(request_ids)
    done = threading.Event()
    pending[msg_id] = [done, None]
    send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
    done.wait(3600)  # like a real agent: wait for the person to answer
    return pending.pop(msg_id)[1]


def update(session_id, body):
    method = "session/update"
    if PASCAL:
        method = "session/notification"
        body = {**body, "sessionUpdate": "".join(w.title() for w in body["sessionUpdate"].split("_"))}
    send({"jsonrpc": "2.0", "method": method, "params": {"sessionId": session_id, "update": body}})


def save_turns():
    if STATE_FILE:
        with open(STATE_FILE, "w") as fh:
            json.dump(turns, fh)


def describe_block(block):
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "resource":
        return f"[resource {block['resource'].get('uri')}]"
    if kind == "image":
        return f"[image {block.get('mimeType')}]"
    if kind == "resource_link":
        return f"[link {block.get('uri')}]"
    return ""


def run_prompt(msg_id, params):
    sid = params["sessionId"]
    text = " ".join(t for t in (describe_block(b) for b in params["prompt"]) if t)
    turns[sid] = turns.get(sid, 0) + 1
    if "crash" in text:
        os._exit(1)

    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking about it"}})
    update(sid, {"sessionUpdate": "plan", "entries": [{"content": "read files", "priority": "high", "status": "in_progress"}]})
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read README.md", "kind": "read", "status": "pending"})
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
    answer = ask_client(
        "session/request_permission",
        {
            "sessionId": sid,
            "toolCall": {"toolCallId": "t2", "title": "Edit main.py", "kind": "edit"},
            "options": [
                {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                {"optionId": "no", "name": "Reject", "kind": "reject_once"},
            ],
        },
    )
    edit = "allowed" if (answer or {}).get("outcome", {}).get("optionId") == "yes" else "denied"
    if edit == "allowed":
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "completed", "content": [
            {"type": "diff", "path": "main.py", "oldText": "print(1)\n", "newText": "print(2)\n"}]})

    if "slow" in text:
        for _ in range(100):
            if sid in cancelled:
                break
            time.sleep(0.05)
    if sid in cancelled:
        cancelled.discard(sid)
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "cancelled"}})
        return

    for chunk in (f"Echo: {text}", f" (turn {turns[sid]}, edit {edit})"):
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": chunk}})
    save_turns()
    send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "EndTurn" if PASCAL else "end_turn"}})


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method is None:
            entry = pending.get(msg.get("id"))
            if entry:
                entry[1] = msg.get("result")
                entry[0].set()
        elif method == "initialize":
            caps = {"loadSession": bool(STATE_FILE), "promptCapabilities": {"image": True, "embeddedContext": True}}
            methods = [{"id": m, "name": f"Sign in with {m}"} for m in AUTH_METHODS]
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": caps, "authMethods": methods,
                "agentInfo": {"name": "demo", "version": "1.0"}}})
        elif method == "authenticate":
            global signed_in
            signed_in = msg["params"]["methodId"] in AUTH_METHODS
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif method == "session/new" and not signed_in:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "Authentication required"}})
        elif method == "session/load":
            saved = json.load(open(STATE_FILE)) if STATE_FILE and os.path.exists(STATE_FILE) else {}
            sid = msg["params"]["sessionId"]
            if sid in saved:
                turns[sid] = saved[sid]
                update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "(replayed history)"}})
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32002, "message": "unknown session"}})
        elif method == "session/new":
            sid = f"s{next(session_ids)}"
            while sid in turns:  # after a reload, don't hand out a used id
                sid = f"s{next(session_ids)}"
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": sid}})
        elif method == "session/prompt":
            threading.Thread(target=run_prompt, args=(msg["id"], msg["params"]), daemon=True).start()
        elif method == "session/cancel":
            cancelled.add(msg["params"]["sessionId"])
        elif "id" in msg:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not found"}})


if __name__ == "__main__":
    main()
