# Configuration reference

`kiro-a2a add` writes this for you. This page is for when you want to edit it by hand.

## Where things live

| File | Written by | Purpose |
|---|---|---|
| `.kiro/a2a-agents.json` (or `~/.kiro/…` with `-g`) | `add` | URL, sign-in, TLS and headers for each agent |
| `.kiro/settings/mcp.json` | `add` | registers `kiro-a2a mcp --config … --only NAME` as an MCP server |
| `.kiro/agents/NAME.json` | `add` | a Kiro custom agent that relays to the A2A agent |
| `.kiro/steering/NAME.md` | `add` | tells Kiro when to delegate and how to continue |
| `%APPDATA%\kiro-a2a\tokens.json` / `~/.config/kiro-a2a/tokens.json` | `login` and sign-ins | OAuth tokens, readable only by you (`KIRO_A2A_HOME` moves it) |

## `a2a-agents.json`

```json
{
  "agents": [
    {
      "name": "reviewer",
      "url": "https://agents.example.com/reviewer",
      "headers": { "X-Team": "platform" },
      "timeout": 600,
      "auth": {
        "type": "oauth2",
        "client_id": "1234-abcd",
        "issuer": "https://login.example.com/my-tenant/v2.0",
        "scopes": ["api://reviewer/use", "offline_access"]
      },
      "tls": { "ca_bundle": "/path/to/company-root.pem" },
      "card_key": "/path/to/reviewer-card-public.pem"
    }
  ]
}
```

Any string may use `${ENV_VAR}`. It's read from the environment Kiro starts the bridge with,
so secrets never sit in the file.

### `auth`

| Key | Meaning |
|---|---|
| `type` | `auto` (default: follow the agent card), `oauth2`, `bearer`, `api_key`, `none` |
| `flow` | `client_credentials`, `authorization_code` (browser + PKCE), `device_code`. Left out: a client secret means client credentials, otherwise browser sign-in |
| `client_id`, `client_secret` | your OAuth app (no secret for public/desktop apps) |
| `issuer` | OpenID Connect issuer. The token, authorization and device endpoints are discovered from it |
| `token_url`, `authorization_url`, `device_authorization_url` | endpoints set by hand |
| `scopes` | list or space-separated string. Defaults to the scopes the agent card requires |
| `audience`, `resource` | extra token-request parameters some providers need |
| `redirect_uri` | fixed browser redirect if your OAuth app requires one, e.g. `http://127.0.0.1:8400/callback` |
| `token_auth` | `post` (default) or `basic`: how the client secret is sent |
| `token` | fixed bearer token (`type: bearer`) |
| `api_key`, `api_key_header` | fixed API key (`type: api_key`). The header defaults to the card's, then `X-API-Key` |

How sign-in behaves:

- Tokens refresh before they expire. On a 401 the bridge refreshes and retries once.
- Inside Kiro, browser sign-in may open by itself. Device-code sign-in only runs through `kiro-a2a login --device`.
- When a person must sign in, the Kiro tool says so and names the command to run.

### `tls`

| Key | Meaning |
|---|---|
| `ca_bundle` | PEM file with your company's root certificates |
| `client_cert`, `client_key` | client certificate for mutual TLS |
| `verify` | `false` disables certificate checks. Only for local testing |

`HTTPS_PROXY` and `NO_PROXY` are honoured.

### `card_key`

A public key (PEM file) or HS256 secret. When set, the bridge refuses the agent unless its card is
signed with that key. `kiro-a2a card NAME --verify-key KEY` checks a card by hand.

## `kiro-a2a add` options

```
kiro-a2a add NAME URL [-g | --dir DIR] [--amazon-q] [--force]
  [--client-id ID] [--client-secret-env VAR] [--issuer URL] [--token-url URL]
  [--authorization-url URL] [--flow FLOW] [--scope S ...] [--audience A] [--resource R]
  [--redirect-uri URI] [--token-env VAR] [--api-key-env VAR] [--auth-type TYPE]
  [--header 'Name: value' ...] [--ca-bundle PEM]
```

`--amazon-q` also registers the agent with Amazon Q Developer (`~/.aws/amazonq/mcp.json`,
or `.amazonq/mcp.json` in a project).

## `kiro-a2a serve` options

```
kiro-a2a serve [--agent kiro|codex|gemini|demo | --agent-cmd "…"] [--workdir DIR]
  [--host 127.0.0.1] [--port 8765] [--public-url URL] [--name N] [--description D]
  [--permissions read-only|allow-all|deny-all|ask] [--permission-timeout 600]
  [--prompt-timeout 900] [--max-parallel 4] [--task-db FILE]
  [--api-key KEY] [--oauth-issuer URL --oauth-audience AUD --oauth-scope S]
  [--push --push-host HOST] [--sign-key PEM --sign-kid KID --sign-alg ES256]
  [--kiro-auth-method ID] [--kiro-env NAME=value]
```

| Option | Default | Use |
|---|---|---|
| `--permissions` | `read-only` | `read-only`, `allow-all`, `deny-all`, or `ask` (the caller approves each tool through `input-required`) |
| `--api-key` / `--oauth-issuer` | open | who may call; the card stays public and advertises the scheme |
| `--task-db` | memory | SQLite file for task history |
| `--push`, `--push-host` | off | allow webhooks, only to these hosts |
| `--sign-key` | unsigned | sign the agent card |
| `--kiro-auth-method`, `--kiro-env` | | choose Kiro's ACP sign-in method or pass profile environment |

Optional extras: `pip install "kiro-a2a-bridge[oauth]"` (token checks, card signing),
`[telemetry]` (OpenTelemetry), `[all]`.
