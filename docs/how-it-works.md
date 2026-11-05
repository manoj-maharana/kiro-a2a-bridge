# How kiro-a2a-bridge works

This page explains the design: the three protocols involved, how messages travel between
them, and the choices behind the defaults.

## Three protocols, three jobs

| Protocol | Who talks | Shape |
|---|---|---|
| **MCP** (Model Context Protocol) | an AI app ↔ its tools | JSON-RPC; the app calls `tools/list` and `tools/call` |
| **ACP** (Agent Client Protocol) | an editor ↔ a coding agent | JSON-RPC over the agent's stdin/stdout; sessions, prompts, streamed updates, permission requests |
| **A2A** (Agent2Agent) | an agent ↔ another agent over the network | JSON-RPC over HTTP; agent cards, tasks, streamed events, artifacts |

Kiro is an ACP *agent*: an editor can drive it. When Kiro itself needs something from
outside, it uses MCP tools. Your organization's agents speak A2A. So "Kiro talks to my
agent" really means **MCP → A2A**, and "my app talks to Kiro" means **A2A → ACP**. The bridge
does both, plus **ACP → A2A** for editors that host ACP agents.

```
            ┌────────────────────── kiro-a2a-bridge ──────────────────────┐
 Kiro ─MCP─▶│ mcp.py ──┐                                                   │
 editor ACP▶│ acp_agent.py ─┼─▶ a2a_client.py ──(auth.py, protocol.py)──────┼─A2A─▶ your agents
            │          │                                                   │
 A2A app ──▶│ server.py ──▶ attachments.py ──▶ acp.py ──────────────────────┼─ACP─▶ kiro-cli acp
            └──────────────────────────────────────────────────────────────┘
```

Everything inside uses one task model (A2A 0.3 JSON). `protocol.py` converts to and from
A2A 1.0 at the edges, so each feature is written once and works with both versions.

## Flow 1: Kiro → your agent

1. `setup-kiro` writes `.kiro/a2a-agents.json` and registers `kiro-a2a mcp` in Kiro's MCP
   settings, plus a custom agent and a steering file.
2. Kiro starts `kiro-a2a mcp` and calls `tools/list`. The bridge reads each agent card and
   offers one tool, `ask_<name>`, whose description comes from the card's description and skills.
3. When Kiro calls the tool, the bridge works out the sign-in (from the config, or from the
   card's `securitySchemes`), gets or refreshes a token, and sends `message/stream` (or
   `message/send` if the agent doesn't stream).
4. While the agent works, its `working` updates become MCP `notifications/progress`, so Kiro
   shows what's happening.
5. The answer comes back as the tool result, ending with
   `context_id: … | task_id: … | state: …`. Kiro passes `context_id` back next time, so the
   agent keeps the conversation. If the state is `input-required`, Kiro asks you and replies
   with the same `task_id`.

## Flow 2: an A2A app → Kiro

1. `serve` starts `kiro-cli acp` once and keeps it running. Each A2A `contextId` gets its own
   ACP session.
2. A message becomes a task. Its parts become ACP prompt blocks: text stays text, text files
   become embedded resources, images become image blocks (if the agent accepts them), and
   links become resource links. Large content is cut and marked.
3. ACP updates stream back as A2A events:
   - answer chunks → the `response` artifact (`append` after the first chunk)
   - thoughts, plan, tool calls → `working` status updates tagged with `metadata.kind`
   - file edits (ACP diff content) → one `diff: <path>` artifact each, with a unified diff and the raw texts
4. When the agent asks to use a tool, the permission policy answers. In `ask` mode the task
   pauses as `input-required` with a `permission_request` data part and a `requestId`. The
   caller's answer must carry the same task (and, if given, request) id, so a late answer
   can't approve a newer question.
5. The turn's result sets the final state (`completed`, `canceled`, `rejected`, `failed`) and
   metadata (`stopReason`, `durationMs`, `toolCalls`, `usage`).

If `kiro-cli` crashes, the running task fails, and the next message starts it again. If the
agent supports `session/load`, the conversation is reopened. Otherwise a new session starts.

## Flow 3: an ACP editor → your agent

`kiro-a2a acp` plays the ACP agent for the editor. A prompt becomes an A2A message (attached
files are inlined). Streamed A2A events become ACP updates: answer text, thoughts, plan, and
edit cards for `diff:` artifacts. If the A2A task pauses on a `permission_request`, the bridge
asks the editor with `session/request_permission` and sends the choice back to the same task.

## Sign-in design

- **The agent card decides by default.** If the card lists an OpenID Connect or OAuth scheme
  with scopes, only a `client_id` (and a secret for service accounts) is needed.
- **The right flow per situation.** Service accounts use client credentials. People use the
  browser (PKCE, loopback redirect on `127.0.0.1`) or a device code over SSH.
- **No secrets in files.** Config values can be `${ENV_VAR}`. Tokens go to a per-user file
  with owner-only permissions.
- **Self-healing.** Tokens refresh before expiry. A 401 triggers one refresh and retry. When a
  person must sign in, the Kiro tool says exactly which command to run.
- **Trust the card.** `card_key` makes the client refuse an agent whose card isn't signed
  with the expected key. `serve --sign-key` produces such cards (detached JWS over canonical JSON).

## Why these defaults

| Default | Reason |
|---|---|
| bind `127.0.0.1` | a coding agent should not be reachable from the network by accident |
| `read-only` permissions | review and explain work out of the box; editing is an explicit choice |
| push notifications off, host allowlist | webhooks let callers make the server send requests; that must be opted into and limited |
| no ACP file-system or terminal access offered | the agent keeps its own tools and safety rules; the bridge adds no new way to touch the machine |
| one dependency | easy to audit, quick to install, works everywhere Python does |

## Testing without accounts

The tests run the whole system against two stand-ins that ship with the repository: the demo
ACP agent (`kiro_a2a/demo_agent.py`, also `serve --agent demo`) and a fake OpenID Connect
provider (`tests/fake_idp.py`) that issues real signed JWTs. Each feature is tested end to end:
sign-in flows, A2A 1.0 and 0.3, streaming, push, persistence and the ACP editor mode.
