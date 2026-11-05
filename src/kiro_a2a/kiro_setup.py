"""`kiro-a2a setup-kiro`: wire an A2A agent into a Kiro workspace.

Writes under <dir>/.kiro/:
  a2a-agents.json       the agent's URL, headers, sign-in and TLS settings
  settings/mcp.json     the bridge as an MCP server, so any Kiro agent can call the tool
  agents/<name>.json    a Kiro custom agent that relays every message to the A2A agent
  steering/<name>.md    tells Kiro when and how to hand work to the A2A agent
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys

import aiohttp

from kiro_a2a.a2a_client import A2AClient
from kiro_a2a.config import CONFIG_NAME, read_config, write_config
from kiro_a2a.mcp import tool_name


def bridge_command() -> tuple[str, list[str]]:
    """Use the `kiro-a2a` script when it is on PATH, else this Python."""
    if shutil.which("kiro-a2a"):
        return "kiro-a2a", []
    return sys.executable, ["-m", "kiro_a2a"]


def server_key(name: str) -> str:
    return tool_name(name, set()).removeprefix("ask_")


def mcp_server_entry(key: str, config_path: str) -> dict:
    command, prefix = bridge_command()
    return {"command": command, "args": [*prefix, "mcp", "--config", config_path, "--only", key]}


def custom_agent(name: str, mcp_entry: dict, description: str, steering_uri: str) -> dict:
    key = server_key(name)
    tool = f"ask_{key}"
    return {
        "name": key,
        "description": f"Talks to the {name} A2A agent. {description}".strip(),
        "prompt": (
            f"You are a relay between the user and the remote A2A agent '{name}'. "
            f"For every user message, call the {tool} tool with the user's request. "
            "The remote agent cannot see this workspace, so include any file contents or "
            "details it needs. Show the agent's answer to the user as it is; do not answer on "
            "your own. Pass the context_id from the last reply on every follow-up. If the agent "
            "asks a question (state input-required), show the question, then send the user's "
            "answer back with the same context_id and task_id."
        ),
        "mcpServers": {key: mcp_entry},
        "includeMcpJson": False,
        "tools": [f"@{key}"],
        "allowedTools": [f"@{key}"],
        "resources": [steering_uri],
    }


def steering(name: str, description: str) -> str:
    tool = f"ask_{server_key(name)}"
    about = f"\nAbout this agent: {description}\n" if description else ""
    return f"""---
inclusion: always
---

# {name} agent

A remote A2A agent called **{name}** is connected through the `{tool}` tool.
{about}
- When the user mentions {name}, or asks for something this agent handles, delegate to
  `{tool}` instead of doing the work yourself.
- The agent cannot see this workspace. Put the file contents, paths and details it needs
  into `message`.
- For follow-ups in the same conversation, pass the `context_id` from the previous reply.
- If the reply says `state: input-required`, ask the user the agent's question, then call
  `{tool}` again with their answer, the same `context_id` and the `task_id`.
- If the tool says a sign-in is needed, tell the user to run `kiro-a2a login {server_key(name)}`.
- Show the agent's answer to the user. Say clearly that it came from {name}.
"""


def fetch_description(entry: dict) -> str:
    """Best effort: read the agent card for a description (${VAR}s are
    expanded only for this request; the files keep the placeholders)."""

    async def run() -> str:
        client = A2AClient.from_config(entry)
        async with aiohttp.ClientSession(trust_env=True) as http:
            card = await client.fetch_card(http)
        return card.get("description") or ""

    try:
        return asyncio.run(run())
    except Exception:
        return ""


def setup_kiro(
    name: str,
    url: str,
    directory: str = ".",
    headers: list[str] | None = None,
    force: bool = False,
    auth: dict | None = None,
    tls: dict | None = None,
    amazon_q: bool = False,
) -> list[str]:
    """Write the Kiro files (and, with amazon_q, Amazon Q's MCP config). Returns the lines to print."""
    key = server_key(name)
    root = os.path.abspath(os.path.expanduser(directory))
    kiro = os.path.join(root, ".kiro")
    in_home = os.path.normcase(root) == os.path.normcase(os.path.expanduser("~"))
    steering_uri = f"file://{'~/' if in_home else ''}.kiro/steering/{key}.md"
    report = []

    entry: dict = {"name": key, "url": url}
    if headers:
        entry["headers"] = {k.strip(): v.strip() for k, _, v in (h.partition(":") for h in headers)}
    if auth:
        entry["auth"] = auth
    if tls:
        entry["tls"] = tls
    config_path = os.path.join(kiro, CONFIG_NAME)
    config = read_config(config_path) if os.path.exists(config_path) else {"agents": []}
    others = [a for a in config.get("agents", []) if a.get("name") != key]
    action = "updated" if len(others) < len(config.get("agents", [])) else "added"
    config["agents"] = [*others, entry]
    write_config(config_path, config)
    report.append(f"{action} agent '{key}' in {config_path}")

    description = fetch_description(entry)
    mcp_entry = mcp_server_entry(key, config_path)

    mcp_path = os.path.join(kiro, "settings", "mcp.json")
    mcp = {}
    if os.path.exists(mcp_path):
        with open(mcp_path, encoding="utf-8") as fh:
            mcp = json.load(fh)
    mcp.setdefault("mcpServers", {})[key] = mcp_entry
    _write(mcp_path, json.dumps(mcp, indent=2) + "\n")
    report.append(f"set MCP server '{key}' in {mcp_path}")

    if amazon_q:
        # Amazon Q Developer reads the same mcpServers format: user-wide or per workspace.
        q_path = (os.path.join(root, ".aws", "amazonq", "mcp.json") if in_home
                  else os.path.join(root, ".amazonq", "mcp.json"))
        q_config = {}
        if os.path.exists(q_path):
            with open(q_path, encoding="utf-8") as fh:
                q_config = json.load(fh)
        q_config.setdefault("mcpServers", {})[key] = mcp_entry
        _write(q_path, json.dumps(q_config, indent=2) + "\n")
        report.append(f"set MCP server '{key}' for Amazon Q in {q_path}")

    for path, content in (
        (os.path.join(kiro, "agents", f"{key}.json"),
         json.dumps(custom_agent(name, mcp_entry, description, steering_uri), indent=2) + "\n"),
        (os.path.join(kiro, "steering", f"{key}.md"), steering(name, description)),
    ):
        if os.path.exists(path) and not force:
            report.append(f"kept existing {path} (use --force to replace)")
        else:
            _write(path, content)
            report.append(f"wrote {path}")

    if not description:
        report.append(f"note: could not read the agent card at {url}; descriptions are generic")
    report += ["", f"In Kiro, the tool is ask_{key}. To talk to the agent directly:",
               f"  kiro-cli chat --agent {key}      (or /agent swap {key} inside a chat)"]
    person_signs_in = auth and auth.get("type", "oauth2") == "oauth2" and (
        auth.get("flow") in ("authorization_code", "device_code") or (not auth.get("flow") and not auth.get("client_secret")))
    if person_signs_in:
        report.append(f"Sign in once before using it:  kiro-a2a login {key}")
    for var in sorted(set(re.findall(r"\$\{(\w+)\}", json.dumps(entry)))):
        if var not in os.environ:
            report.append(f"note: set the environment variable {var} where Kiro runs (then restart Kiro)")
    return report


def _paths(directory: str) -> tuple[str, str, bool]:
    root = os.path.abspath(os.path.expanduser(directory))
    return root, os.path.join(root, ".kiro"), os.path.normcase(root) == os.path.normcase(os.path.expanduser("~"))


def list_agents(directory: str = ".") -> list[dict]:
    """Agents set up in this workspace (or ~), with what Kiro has for each."""
    _, kiro, _ = _paths(directory)
    config_path = os.path.join(kiro, CONFIG_NAME)
    if not os.path.exists(config_path):
        return []
    mcp_path = os.path.join(kiro, "settings", "mcp.json")
    servers = {}
    if os.path.exists(mcp_path):
        with open(mcp_path, encoding="utf-8") as fh:
            servers = json.load(fh).get("mcpServers", {})
    rows = []
    for entry in read_config(config_path).get("agents", []):
        key = entry["name"]
        rows.append({
            "name": key,
            "url": entry["url"],
            "auth": (entry.get("auth") or {}).get("type", "auto"),
            "tool": f"ask_{key}",
            "in_kiro": key in servers,
            "custom_agent": os.path.exists(os.path.join(kiro, "agents", f"{key}.json")),
        })
    return rows


def remove_agent(name: str, directory: str = ".") -> list[str]:
    """Undo `add`: remove the agent from every file it was written to."""
    key = server_key(name)
    root, kiro, in_home = _paths(directory)
    report = []

    def drop_server(path: str, label: str) -> None:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("mcpServers", {}).pop(key, None) is not None:
                _write(path, json.dumps(data, indent=2) + "\n")
                report.append(f"removed '{key}' from {label} ({path})")

    config_path = os.path.join(kiro, CONFIG_NAME)
    if os.path.exists(config_path):
        config = read_config(config_path)
        kept = [a for a in config.get("agents", []) if a.get("name") != key]
        if len(kept) != len(config.get("agents", [])):
            config["agents"] = kept
            write_config(config_path, config)
            report.append(f"removed '{key}' from {config_path}")
    drop_server(os.path.join(kiro, "settings", "mcp.json"), "Kiro MCP settings")
    drop_server(os.path.join(root, ".aws", "amazonq", "mcp.json") if in_home
                else os.path.join(root, ".amazonq", "mcp.json"), "Amazon Q")
    for path in (os.path.join(kiro, "agents", f"{key}.json"), os.path.join(kiro, "steering", f"{key}.md")):
        if os.path.exists(path):
            os.remove(path)
            report.append(f"deleted {path}")
    return report or [f"nothing to remove: no agent named '{key}' in {kiro}"]


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
