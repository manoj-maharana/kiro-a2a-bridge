"""The agents config file: which A2A agents to use and how to sign in to them.

{
  "agents": [
    {
      "name": "reviewer",
      "url": "https://agents.example.com/reviewer",
      "headers": {"X-Team": "platform"},
      "timeout": 600,
      "auth": {"type": "oauth2", "client_id": "...", "issuer": "https://login.example.com", "scopes": ["..."]},
      "tls": {"ca_bundle": "/path/to/company-root.pem"}
    }
  ]
}

Strings may use ${ENV_VAR}; they are expanded when the file is read, so
secrets can stay out of the file.
"""

from __future__ import annotations

import json
import os

CONFIG_NAME = "a2a-agents.json"


def default_paths() -> list[str]:
    """Workspace file first, then the user-wide one (once, if they are the same file)."""
    paths = [os.path.join(".kiro", CONFIG_NAME), os.path.join(os.path.expanduser("~"), ".kiro", CONFIG_NAME)]
    if os.path.normcase(os.path.abspath(paths[0])) == os.path.normcase(os.path.abspath(paths[1])):
        return paths[1:]
    return paths


def read_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    if not isinstance(config.get("agents", []), list):
        raise ValueError(f"{path}: 'agents' must be a list")
    return config


def write_config(path: str, config: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")


def find_agent(name: str, path: str | None = None) -> tuple[dict, str]:
    """Look an agent up by name in the given config, or in the default places."""
    paths = [path] if path else [p for p in default_paths() if os.path.exists(p)]
    for candidate in paths:
        for entry in read_config(candidate).get("agents", []):
            if entry.get("name") == name:
                return entry, candidate
    where = path or " or ".join(default_paths())
    raise SystemExit(f"No agent named '{name}' in {where}. Add it with `kiro-a2a setup-kiro`.")
