"""Command line for kiro-a2a.

  Kiro -> your A2A agents:   setup-kiro, login, logout, mcp, ask, doctor
  ACP editors -> A2A agent:  acp
  A2A apps -> Kiro:          serve
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import sys

import aiohttp
from aiohttp import web

from kiro_a2a import __version__
from kiro_a2a.a2a_client import A2AClient, A2AError
from kiro_a2a.acp import POLICIES, AcpAgent
from kiro_a2a.auth import FLOWS, AuthError
from kiro_a2a.config import default_paths, find_agent, read_config
from kiro_a2a.presets import PRESETS


def parse_header(value: str) -> tuple[str, str]:
    name, sep, val = value.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(f"header must look like 'Name: value', got {value!r}")
    return name.strip(), os.path.expandvars(val.strip())


def parse_env(value: str) -> tuple[str, str]:
    name, sep, val = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"expected NAME=value, got {value!r}")
    return name, val


def load_agents(args) -> list[A2AClient]:
    """Agents come from --agent (URL or name=URL) and/or a --config file."""
    shared_headers = dict(args.header or [])
    clients = []
    for value in args.agent:
        name, sep, url = value.partition("=")
        if not sep or name.startswith(("http://", "https://")):
            name, url = None, value
        clients.append(A2AClient(url, name=name, headers=shared_headers, timeout=args.timeout))
    if args.config:
        for entry in read_config(args.config).get("agents", []):
            if args.only and entry.get("name") not in args.only:
                continue
            clients.append(A2AClient.from_config(entry, shared_headers, args.timeout))
    if not clients:
        sys.exit("kiro-a2a: give at least one --agent URL or a --config file with agents")
    return clients


def client_for(target: str, args) -> A2AClient:
    """A URL, or the name of an agent in a config file."""
    if target.startswith(("http://", "https://")):
        return A2AClient(target, headers=dict(args.header or []), timeout=args.timeout)
    entry, _ = find_agent(target, args.config)
    return A2AClient.from_config(entry, dict(args.header or []), args.timeout)


def http_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(trust_env=True)  # honours HTTPS_PROXY / NO_PROXY


# ---- commands ---------------------------------------------------------------

def cmd_serve(args) -> None:
    from kiro_a2a.presets import preset
    from kiro_a2a.push import PushNotifier
    from kiro_a2a.server import Bridge, OAuthValidator, make_app

    chosen = preset(args.agent)
    command = shlex.split(args.agent_cmd, posix=os.name != "nt") if args.agent_cmd else chosen["command"]
    agent = AcpAgent(
        command,
        cwd=args.workdir,
        permissions=args.permissions,
        env=dict(args.kiro_env or []) or None,
        auth_method=args.kiro_auth_method,
    )
    host_for_url = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    public_url = args.public_url or f"http://{host_for_url}:{args.port}/"
    store = None
    if args.task_db:
        from kiro_a2a.store import SqliteTaskStore

        store = SqliteTaskStore(args.task_db)
    signer = None
    if args.sign_key:
        from kiro_a2a.card_signing import CardSigner, load_key

        signer = CardSigner(load_key(os.path.expandvars(args.sign_key)), args.sign_kid, args.sign_alg)
    oauth = None
    if args.oauth_issuer:
        oauth = OAuthValidator(args.oauth_issuer, args.oauth_audience, args.oauth_scope or [], args.oauth_jwks_url)
    bridge = Bridge(
        agent,
        name=args.name or chosen["name"],
        description=args.description or f"{chosen['name']} coding agent, reachable over A2A.",
        public_url=public_url,
        api_key=args.api_key or os.environ.get("KIRO_A2A_API_KEY"),
        oauth=oauth,
        prompt_timeout=args.prompt_timeout,
        max_parallel=args.max_parallel,
        permission_timeout=args.permission_timeout,
        store=store,
        push=PushNotifier(args.push, args.push_host),
        signer=signer,
    )
    logging.getLogger(__name__).info(
        "A2A agent card: %s.well-known/agent-card.json (workdir %s, permissions %s)",
        public_url, agent.cwd, args.permissions,
    )
    web.run_app(make_app(bridge), host=args.host, port=args.port, print=None)


def cmd_mcp(args) -> None:
    from kiro_a2a.mcp import McpServer, serve_stdio

    asyncio.run(serve_stdio(McpServer(load_agents(args))))


def cmd_acp(args) -> None:
    from kiro_a2a.acp_agent import serve_acp

    clients = load_agents(args)
    if len(clients) != 1:
        sys.exit("kiro-a2a acp: choose exactly one agent (use --only NAME with --config)")
    asyncio.run(serve_acp(clients[0]))


def cmd_ask(args) -> None:
    import json
    import time

    from kiro_a2a.console import Console

    console = Console()

    async def run() -> int:
        client = client_for(args.agent, args)
        client.auth.allow_device = True  # we have a terminal

        async def show_progress(text: str, kind: str | None = None) -> None:
            if not args.json:
                console.progress(text, kind)

        started = time.monotonic()
        async with http_session() as http:
            card = await client.fetch_card(http)
            if not args.json:
                console.header(card.get("name") or client.url, client.wire, client.rpc_url, bool(card.get("signatures")))
            reply = await client.ask(http, args.message, args.context_id, args.task_id, show_progress, args.file)
        ok = reply.state in ("completed", "input-required")
        if args.json:
            print(json.dumps({"text": reply.text, "state": reply.state, "context_id": reply.context_id,
                              "task_id": reply.task_id, "metadata": reply.metadata}, indent=2, ensure_ascii=False))
            return 0 if ok else 1
        console.line()
        print(reply.text, flush=True)
        console.summary(reply.state, time.monotonic() - started, reply.metadata)
        console.line(console.style(f"  context {reply.context_id}  task {reply.task_id}", "dim"))
        target = f'"{args.agent}"' if " " in args.agent else args.agent
        if reply.state == "input-required":
            console.line(console.style("  reply:     ", "yellow") + f'kiro-a2a ask {target} "<your answer>" '
                         f"--context-id {reply.context_id} --task-id {reply.task_id}")
        elif reply.state == "completed":
            console.line(console.style(f'  follow up: kiro-a2a ask {target} "..." --context-id {reply.context_id}', "dim"))
        return 0 if ok else 1

    sys.exit(asyncio.run(run()))


def cmd_login(args) -> None:
    async def run() -> None:
        client = client_for(args.agent, args)
        if args.device:
            client.auth.config.flow = "device_code"
        async with http_session() as http:
            card = await client.fetch_card(http)
            await client.auth.login(http, card)
        print(f"Signed in to {card.get('name') or client.url}.")

    asyncio.run(run())


def cmd_logout(args) -> None:
    async def run() -> None:
        client = client_for(args.agent, args)
        async with http_session() as http:
            await client.auth.resolve(http, await client.fetch_card(http))
        client.auth.logout()
        print("Signed out; the saved token was removed.")

    asyncio.run(run())


def cmd_card(args) -> None:
    import json

    async def run() -> None:
        client = client_for(args.agent, args)
        if args.verify_key:
            client.card_key = args.verify_key
        async with http_session() as http:
            card = await client.fetch_card(http)
        print(json.dumps(card, indent=2, ensure_ascii=False))
        if args.verify_key:
            note = "signature verified"
        else:
            note = "signed (not checked)" if card.get("signatures") else "not signed"
        print(f"\n[{card.get('name')} | A2A {client.wire} | {note}]", file=sys.stderr)

    asyncio.run(run())


def cmd_doctor(args) -> None:
    from kiro_a2a.doctor import run_doctor

    sys.exit(asyncio.run(run_doctor(args)))


def cmd_setup_kiro(args) -> None:
    from kiro_a2a.kiro_setup import setup_kiro

    auth = None
    if args.client_id or args.auth_type or args.api_key_env or args.token_env:
        implied = "api_key" if args.api_key_env else "bearer" if args.token_env else "oauth2"
        auth = {"type": args.auth_type or implied}
        for key in ("client_id", "issuer", "token_url", "authorization_url", "audience", "resource", "redirect_uri"):
            if getattr(args, key):
                auth[key] = getattr(args, key)
        if args.flow:
            auth["flow"] = args.flow
        if args.scope:
            auth["scopes"] = args.scope
        if args.client_secret_env:
            auth["client_secret"] = "${" + args.client_secret_env + "}"
        if args.token_env:
            auth["token"] = "${" + args.token_env + "}"
        if args.api_key_env:
            auth["api_key"] = "${" + args.api_key_env + "}"
    tls = {"ca_bundle": os.path.abspath(args.ca_bundle)} if args.ca_bundle else None
    for line in setup_kiro(args.name, args.url, _where(args), args.header or [], args.force, auth, tls, args.amazon_q):
        print(line)


def _where(args) -> str:
    return "~" if args.global_ else args.dir


def cmd_list(args) -> None:
    from kiro_a2a.kiro_setup import list_agents

    rows = list_agents(_where(args))
    if not rows:
        print(f"No agents yet. Add one:  kiro-a2a add <name> <url>{' --global' if args.global_ else ''}")
        return
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        state = "in Kiro" if r["in_kiro"] else "NOT in Kiro settings"
        print(f"{r['name']:<{width}}  {r['url']}  auth={r['auth']}  tool={r['tool']}  ({state})")


def cmd_remove(args) -> None:
    from kiro_a2a.kiro_setup import remove_agent

    for line in remove_agent(args.name, _where(args)):
        print(line)


def _where_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dir", default=".", help="workspace folder (default: current folder)")
    p.add_argument("-g", "--global", dest="global_", action="store_true", help="for all projects (~/.kiro)")


# ---- argument parsing -------------------------------------------------------

def _agent_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent", action="append", default=[], metavar="[NAME=]URL", help="A2A agent URL; repeat for more")
    p.add_argument("--config", help=f"agents config file (see README); usually .kiro/{os.path.basename(default_paths()[0])}")
    p.add_argument("--only", action="append", metavar="NAME", help="use only these agents from --config")
    p.add_argument("--header", action="append", type=parse_header, metavar="'Name: value'", help="sent to every --agent")
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait for an answer (default 600)")


def _target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("agent", help="agent URL, or its name in the agents config")
    p.add_argument("--config", help="agents config file (default: .kiro/a2a-agents.json, then ~/.kiro/a2a-agents.json)")
    p.add_argument("--header", action="append", type=parse_header, metavar="'Name: value'")
    p.add_argument("--timeout", type=float, default=600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiro-a2a", description="Use your A2A agents from Kiro. Run `kiro-a2a add NAME URL` to start.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging (to stderr)")
    sub = parser.add_subparsers(dest="command", required=True)

    # Kiro -> A2A
    setup = sub.add_parser("add", aliases=["setup-kiro"],
                           help="connect an A2A agent to Kiro (writes the .kiro files)")
    setup.add_argument("name", help="short name for the agent, e.g. reviewer")
    setup.add_argument("url", help="the A2A agent's URL")
    _where_args(setup)
    setup.add_argument("--header", action="append", metavar="'Name: value'",
                       help="stored as written; use ${VAR} for secrets")
    auth = setup.add_argument_group("sign-in (all optional; without them the agent card decides)")
    auth.add_argument("--auth-type", choices=("oauth2", "bearer", "api_key", "none"))
    auth.add_argument("--client-id", help="OAuth client id")
    auth.add_argument("--client-secret-env", metavar="VAR", help="env var that holds the OAuth client secret")
    auth.add_argument("--issuer", help="OpenID Connect issuer URL (endpoints are discovered)")
    auth.add_argument("--token-url")
    auth.add_argument("--authorization-url")
    auth.add_argument("--flow", choices=FLOWS)
    auth.add_argument("--scope", action="append", help="OAuth scope; repeat for more")
    auth.add_argument("--audience")
    auth.add_argument("--resource")
    auth.add_argument("--redirect-uri", help="fixed redirect URI, if your OAuth app needs one (http://127.0.0.1:PORT/callback)")
    auth.add_argument("--token-env", metavar="VAR", help="env var that holds a fixed bearer token")
    auth.add_argument("--api-key-env", metavar="VAR", help="env var that holds an API key")
    setup.add_argument("--ca-bundle", help="PEM file with your company's root certificates")
    setup.add_argument("--force", action="store_true", help="replace existing agent and steering files")
    setup.add_argument("--amazon-q", action="store_true",
                       help="also register the agent with Amazon Q Developer (VS Code / JetBrains / Q CLI)")
    setup.set_defaults(func=cmd_setup_kiro)

    listing = sub.add_parser("list", aliases=["ls"], help="show the agents connected to Kiro")
    _where_args(listing)
    listing.set_defaults(func=cmd_list)

    remove = sub.add_parser("remove", aliases=["rm"], help="disconnect an agent from Kiro (undoes add)")
    remove.add_argument("name")
    _where_args(remove)
    remove.set_defaults(func=cmd_remove)

    login = sub.add_parser("login", help="sign in to an agent that uses OAuth (opens the browser)")
    _target_args(login)
    login.add_argument("--device", action="store_true", help="use a code instead of a browser (for SSH sessions)")
    login.set_defaults(func=cmd_login)

    logout = sub.add_parser("logout", help="remove the saved sign-in for an agent")
    _target_args(logout)
    logout.set_defaults(func=cmd_logout)

    ask = sub.add_parser("ask", help="send one message to an A2A agent and print the reply")
    _target_args(ask)
    ask.add_argument("message")
    ask.add_argument("--context-id")
    ask.add_argument("--task-id", help="answer a question the agent asked (input-required)")
    ask.add_argument("--file", action="append", metavar="PATH", help="attach a file; repeat for more")
    ask.add_argument("--json", action="store_true", help="print the result as JSON (for scripts)")
    ask.set_defaults(func=cmd_ask)

    card = sub.add_parser("card", help="show an agent's card (and check its signature)")
    _target_args(card)
    card.add_argument("--verify-key", metavar="PEM-or-SECRET",
                      help="public key file or HS256 secret to check the signature")
    card.set_defaults(func=cmd_card)

    doctor = sub.add_parser("doctor", help="check agents, sign-in and kiro-cli")
    doctor.add_argument("--config", help="agents config file (default: the usual places)")
    doctor.add_argument("--kiro-cmd", default="kiro-cli acp", help="ACP command to check (default: kiro-cli acp)")
    doctor.set_defaults(func=cmd_doctor)

    mcp = sub.add_parser("mcp", help="MCP server for Kiro (Kiro starts this; written by add)")
    _agent_source_args(mcp)
    mcp.set_defaults(func=cmd_mcp)

    # ACP editors -> A2A
    acp = sub.add_parser("acp", help="present one A2A agent as an ACP agent, for ACP editors")
    _agent_source_args(acp)
    acp.set_defaults(func=cmd_acp)

    # A2A -> Kiro
    serve = sub.add_parser("serve", help="expose Kiro CLI (or another ACP agent) as an A2A agent")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--workdir", default=".", help="folder Kiro works in (default: current folder)")
    serve.add_argument("--agent", default="kiro", choices=sorted(PRESETS),
                       help="which ACP agent to run (default: kiro; 'demo' needs nothing installed)")
    serve.add_argument("--agent-cmd", "--kiro-cmd", dest="agent_cmd",
                       help="command that starts any other ACP agent (overrides --agent)")
    serve.add_argument("--kiro-env", action="append", type=parse_env, metavar="NAME=value",
                       help="extra environment for kiro-cli, e.g. to pick a sign-in profile")
    serve.add_argument("--kiro-auth-method", metavar="ID",
                       help="ACP sign-in method to use (see `kiro-a2a doctor` for the list)")
    serve.add_argument("--permissions", choices=POLICIES, default="read-only",
                       help="what to answer when Kiro asks to use a tool (default: read-only; "
                       "'ask' passes the question to the caller as input-required)")
    serve.add_argument("--prompt-timeout", type=float, default=900, help="seconds per message (default 900)")
    serve.add_argument("--max-parallel", type=int, default=4, help="messages Kiro works on at once (default 4)")
    serve.add_argument("--permission-timeout", type=float, default=600,
                       help="in 'ask' mode, seconds to wait for an answer before denying the tool (default 600)")
    serve.add_argument("--name", help="agent name shown in the agent card")
    serve.add_argument("--description", help="agent description shown in the agent card")
    serve.add_argument("--task-db", metavar="FILE", help="keep tasks in this SQLite file (default: memory)")
    push = serve.add_argument_group("push notifications (off by default)")
    push.add_argument("--push", action="store_true", help="allow callers to register webhooks")
    push.add_argument("--push-host", action="append", metavar="HOST",
                      help="only send webhooks to this host (wildcards like *.example.com); repeat for more")
    sign = serve.add_argument_group("signed agent card")
    sign.add_argument("--sign-key", metavar="PEM-or-SECRET", help="private key file (ES256/RS256) or HS256 secret")
    sign.add_argument("--sign-kid", default="card-key", help="key id put in the signature (default: card-key)")
    sign.add_argument("--sign-alg", default="ES256", choices=("ES256", "ES384", "RS256", "PS256", "HS256"))
    serve.add_argument("--public-url", help="URL callers should use, if different from host:port")
    guard = serve.add_argument_group("who may call (either one is enough)")
    guard.add_argument("--api-key", help="require 'Authorization: Bearer <key>' (or set KIRO_A2A_API_KEY)")
    guard.add_argument("--oauth-issuer", help="accept OAuth access tokens (JWT) from this OpenID Connect issuer")
    guard.add_argument("--oauth-audience", help="required token audience")
    guard.add_argument("--oauth-scope", action="append", help="required scope; repeat for more")
    guard.add_argument("--oauth-jwks-url", help="signing keys URL, if not found through discovery")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # stderr only: in `mcp` and `acp` mode stdout belongs to the protocol.
    # Servers log their activity; one-shot commands stay quiet unless -v.
    quiet = args.command not in ("serve", "mcp", "acp")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if quiet else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass
    except (A2AError, AuthError, OSError, ValueError) as exc:
        if args.verbose:
            raise
        sys.exit(f"kiro-a2a: {exc}")


if __name__ == "__main__":
    main()
