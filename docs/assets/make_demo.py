"""Builds demo.svg: an animated terminal showing kiro-a2a end to end.

    python docs/assets/make_demo.py

Commands are typed out, output lines appear in order, and the whole thing
loops. Pure SVG + CSS, so it plays inside <img> on GitHub and PyPI.
"""

from html import escape
from pathlib import Path

CYCLE = 18.0       # seconds per loop
CHAR_W = 8.43      # width of one character at 14px in the fonts listed below
LINE_H = 21
TOP, LEFT = 62, 22

C = {"fg": "#1e293b", "dim": "#64748b", "prompt": "#7c3aed", "cmd": "#0f172a", "ok": "#16a34a",
     "teal": "#0d9488", "amber": "#d97706", "agent": "#7c3aed"}
BG, BAR = "#ffffff", "#f1f5f9"

# (delay before the line, kind, [(text, colour), ...]); kind "cmd" lines are typed
SCRIPT = [
    (0.4, "cmd", [("$ ", "prompt"), ("pip install kiro-a2a-bridge", "cmd")]),
    (0.5, "out", [("Successfully installed kiro-a2a-bridge", "dim")]),
    (0.6, "cmd", [("$ ", "prompt"), ("kiro-a2a add orchestrator http://localhost:9100 --global", "cmd")]),
    (0.5, "out", [("added agent 'orchestrator' in ~/.kiro/a2a-agents.json", "dim")]),
    (0.1, "out", [("set MCP server 'orchestrator' in ~/.kiro/settings/mcp.json", "dim")]),
    (0.1, "out", [("In Kiro, the tool is ", "fg"), ("ask_orchestrator", "teal")]),
    (0.6, "cmd", [("$ ", "prompt"), ("kiro-a2a doctor", "cmd")]),
    (0.5, "out", [("  [ok  ] ", "ok"), ("agent card: Team Orchestrator (streaming: yes)", "fg")]),
    (0.15, "out", [("  [ok  ] ", "ok"), ("sign-in: oauth2, authorization_code", "fg")]),
    (0.6, "cmd", [("$ ", "prompt"), ('kiro-a2a ask orchestrator "calculate (12 + 30) * 2 and then uppercase: hello team"', "cmd")]),
    (0.5, "out", [("◆ ", "agent"), ("Team Orchestrator", "cmd"), ("  A2A 1.0 · http://localhost:9100/", "dim")]),
    (0.4, "out", [("  · ", "dim"), ("orchestrator: plan = ['calculator', 'text-tools']", "dim")]),
    (0.4, "out", [("  · ", "dim"), ("orchestrator: asking calculator over A2A", "dim")]),
    (0.4, "out", [("  · ", "dim"), ("  calculator: expression is '(12 + 30) * 2'", "dim")]),
    (0.4, "out", [("  · ", "dim"), ("orchestrator: asking text-tools over A2A", "dim")]),
    (0.4, "out", [("  · ", "dim"), ("  text-tools: operation 'uppercase'", "dim")]),
    (0.5, "out", [("Results from my sub-agents:", "fg")]),
    (0.1, "out", [("- calculator: (12 + 30) * 2 = ", "fg"), ("84", "teal")]),
    (0.1, "out", [("- text-tools: uppercase: ", "fg"), ("HELLO TEAM", "teal")]),
    (0.4, "out", [("✓ completed", "ok"), (" · 0.4s", "dim")]),
    (0.1, "out", [('  follow up: kiro-a2a ask orchestrator "..." --context-id 86e4c892', "dim")]),
]
TYPE_SPEED = 0.028  # seconds per typed character


def pct(t: float) -> str:
    return f"{min(t / CYCLE * 100, 100):.3f}%"


def build() -> str:
    lines, styles = [], []
    t = 0.0
    hold_end = CYCLE - 0.8
    for i, (delay, kind, segments) in enumerate(SCRIPT):
        t += delay
        y = TOP + i * LINE_H
        text = "".join(s for s, _ in segments)
        spans = "".join(f'<tspan fill="{C[c]}">{escape(s)}</tspan>' for s, c in segments)
        styles.append(
            f"@keyframes l{i}{{0%,{pct(t)}{{opacity:0}}{pct(t + 0.01)},{pct(hold_end)}{{opacity:1}}100%{{opacity:0}}}}"
            f".l{i}{{animation:l{i} {CYCLE}s linear infinite}}"
        )
        if kind == "cmd":
            # Typing: a background-coloured cover slides off the text. If a viewer
            # can't animate, the cover's resting position leaves the text readable.
            width = len(text) * CHAR_W + 6
            typed = len(text) * TYPE_SPEED
            styles.append(
                f"@keyframes w{i}{{0%,{pct(t)}{{transform:translateX(0)}}"
                f"{pct(t + typed)},100%{{transform:translateX({width:.0f}px)}}}}"
                f".w{i}{{animation:w{i} {CYCLE}s steps({len(text)},end) infinite}}"
            )
            lines.append(
                f'<text class="l{i}" x="{LEFT}" y="{y}">{spans}</text>'
                f'<rect class="w{i}" x="{LEFT}" y="{y - 15}" width="{width:.0f}" height="{LINE_H}" '
                f'fill="{BG}" transform="translate({width:.0f} 0)"/>'
            )
            t += typed
        else:
            lines.append(f'<text class="l{i}" x="{LEFT}" y="{y}">{spans}</text>')
    height = TOP + len(SCRIPT) * LINE_H + 10
    width = 900
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Terminal: install kiro-a2a, add an A2A agent to Kiro, check it, and ask it a question">
<style>
text{{font-family:'JetBrains Mono','Cascadia Code','SF Mono',Consolas,'DejaVu Sans Mono',monospace;font-size:14px;white-space:pre}}
{"".join(styles)}
</style>
<rect width="{width}" height="{height}" rx="14" fill="{BG}"/>
<rect width="{width}" height="36" rx="14" fill="{BAR}"/><rect y="22" width="{width}" height="14" fill="{BAR}"/>
<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="14" fill="none" stroke="#e2e8f0"/>
<circle cx="22" cy="18" r="6" fill="#f87171"/><circle cx="42" cy="18" r="6" fill="#fbbf24"/><circle cx="62" cy="18" r="6" fill="#4ade80"/>
<text x="{width / 2}" y="23" text-anchor="middle" fill="{C['dim']}" style="font-size:12.5px">kiro-a2a: your A2A agents in Kiro</text>
{"".join(lines)}
</svg>
"""


if __name__ == "__main__":
    out = Path(__file__).with_name("demo.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out}")
