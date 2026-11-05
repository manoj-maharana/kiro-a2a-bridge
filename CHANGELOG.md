# Changelog

## 0.1.0 (unreleased)

First release.

**Fixed during end-to-end testing with Kiro**
- Answering an agent's question after it closed no longer errors: the finished result is returned with a note.
- A reply naming an unknown task is rejected (`TaskNotFound`) instead of silently starting a new task.
- Permission questions no longer show their machine-readable JSON in Kiro or the terminal.
- Answers come before file diffs.
- Missing secret environment variables are named in the error, and `add` warns about them.
- `add` only suggests `login` when a person actually signs in (not for service accounts).
- Clearer errors when an agent is down, and a clean one-line error in the CLI instead of a traceback.
- Quieter CLI output (no routine log lines unless `-v`).

**Kiro → your A2A agents**
- `setup-kiro`: one command adds an A2A agent to Kiro (MCP server, custom agent, steering file).
- MCP server with one `ask_<agent>` tool per agent, live progress, follow-ups (`context_id`),
  agent questions (`input-required`).
- Sign-in: OAuth 2.0 client credentials, browser sign-in with PKCE, device code; OpenID
  Connect discovery; bearer tokens; API keys; settings read from the agent card. Tokens
  cached and refreshed. Custom CA bundles, client certificates, proxies.
- `login`, `logout`, `doctor`, `ask` (with `--file` attachments), `card` (with signature check).

**ACP editors → your A2A agent**
- `acp`: an A2A agent as an ACP agent. Streaming text, thoughts, plan, edit cards for diffs,
  permission prompts, cancel.

**A2A apps → Kiro or any ACP agent (`serve`)**
- A2A 1.0 and 0.3 on the same endpoint. Streaming, `tasks/get`, `tasks/list`,
  `tasks/cancel`, resubscribe.
- Presets: `kiro`, `codex`, `gemini`, `demo` (built in, offline), or `--agent-cmd`.
- Attachments (text files, images, links), diff artifacts per edited file, usage metadata.
- Permissions: `read-only`, `allow-all`, `deny-all`, `ask` (input-required, answers bound to a request id).
- Caller auth: API key and/or OAuth JWT checks against an OpenID Connect issuer.
- Signed agent cards (ES256, RS256, HS256).
- Push notifications (off by default; host allowlist).
- SQLite task store (`--task-db`).
- Session reload after an agent crash; prompt timeout; parallel limit.
- Optional OpenTelemetry tracing with `traceparent` propagation.
- Docker image; releases to PyPI through trusted publishing.
