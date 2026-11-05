"""Ready-made commands for ACP agents that `serve` can put behind A2A.

`--agent-cmd` overrides any of these, so any ACP agent works.
"""

from __future__ import annotations

import os
import sys

PRESETS: dict[str, dict] = {
    "kiro": {"command": ["kiro-cli", "acp"], "name": "Kiro",
             "needs": "Kiro CLI, signed in (kiro-cli login)"},
    "codex": {"command": ["npx", "-y", "@zed-industries/codex-acp"], "name": "Codex",
              "needs": "Node.js and an OpenAI API key"},
    "gemini": {"command": ["gemini", "--experimental-acp"], "name": "Gemini CLI",
               "needs": "Gemini CLI, signed in"},
    "demo": {"command": [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_agent.py")],
             "name": "Demo agent", "needs": "nothing (built in, offline)"},
}


def preset(name: str) -> dict:
    if name not in PRESETS:
        raise SystemExit(f"unknown agent '{name}'; choose one of: {', '.join(PRESETS)}")
    return PRESETS[name]
