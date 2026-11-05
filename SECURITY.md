# Security

## Reporting a problem

Please report vulnerabilities privately through GitHub's "Report a vulnerability" button
(Security tab), not in public issues. You'll get a reply within a week.

## How the bridge protects you

**Calling your agents (`mcp`, `acp`, `ask`)**
- Secrets in config files are `${ENV_VAR}` placeholders, read at run time.
- OAuth tokens are stored in a file only your user can read, and refreshed rather than re-entered.
- Browser sign-in uses PKCE and a random `state`. The redirect listener binds to `127.0.0.1` only.
- TLS certificates are checked. `tls.verify: false` exists for local testing only.
- With `card_key`, the bridge refuses agents whose card isn't signed by the expected key.

**Exposing an agent (`serve`)**
- Binds to `127.0.0.1` unless you pass `--host`.
- Caller authentication: `--api-key` and/or `--oauth-issuer` (JWT checks: signature, issuer,
  expiry, audience, scopes). The agent card stays public, as A2A expects.
- Tool permissions default to `read-only`. `ask` mode pauses for the caller's approval, and
  answers are bound to the question's request id.
- The bridge gives the agent no file-system or terminal access of its own. The agent uses its
  own tools, under its own rules, inside `--workdir`.
- Push notifications are off unless you pass `--push`. Use `--push-host` to limit where
  webhooks can go, so callers can't make the server send requests to internal addresses.
- Prompt timeout and a parallel limit bound how much work one caller can start.

**What the bridge does not do**
- It does not sandbox the agent. With `allow-all`, the agent can change anything its own
  tools can reach. Run it in a container or a throwaway checkout.
- It does not change where the agent sends your code. Kiro and other hosted agents send
  prompts and code to their providers.
