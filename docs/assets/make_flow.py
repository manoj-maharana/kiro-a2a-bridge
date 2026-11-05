"""Builds flow.svg: one request end to end, revealed step by step.

    python docs/assets/make_flow.py
"""

from html import escape
from pathlib import Path

CYCLE = 16.0
W, H = 1000, 640
LANES = [("Kiro", 135, "#ffffff", "#c4b5fd"), ("kiro-a2a", 385, "#7c3aed", "#7c3aed"),
         ("orchestrator", 635, "#ffffff", "#5eead4"), ("sub-agent", 885, "#f0fdfa", "#99f6e4")]
COLOURS = {"req": "#7c3aed", "back": "#0d9488", "ask": "#d97706"}

# (from lane, to lane, label, kind, step badge or None)
STEPS = [
    (0, 1, "tools/call ask_orchestrator(\"…\")", "req", "1"),
    (1, 2, "sign in (OAuth) · SendStreamingMessage", "req", None),
    (2, 3, "SendMessage over A2A", "req", None),
    (3, 2, "progress + answer", "back", None),
    (2, 1, "working updates (streamed)", "back", None),
    (1, 0, "live progress in the chat", "back", "2"),
    (2, 1, "input-required: \"Which numbers?\"", "ask", None),
    (1, 0, "Kiro asks you the question", "ask", "3"),
    (0, 1, "your answer, same task_id", "req", "4"),
    (1, 2, "resume the task", "req", None),
    (2, 1, "completed", "back", None),
    (1, 0, "answer + context_id for follow-ups", "back", "5"),
]


def pct(t: float) -> str:
    return f"{min(t / CYCLE * 100, 100):.2f}%"


def build() -> str:
    styles, body = [], []
    top, gap = 168, 36
    for i, (a, b, label, kind, badge) in enumerate(STEPS):
        t = 0.6 + i * 0.9
        styles.append(f"@keyframes s{i}{{0%,{pct(t)}{{opacity:0}}{pct(t + 0.35)},{pct(CYCLE - 1)}{{opacity:1}}100%{{opacity:0}}}}"
                      f".s{i}{{animation:s{i} {CYCLE}s ease-out infinite}}")
        y = top + i * gap
        x1, x2 = LANES[a][1], LANES[b][1]
        x1 += 8 if x2 > x1 else -8
        x2 += -10 if x2 > x1 else 10
        colour = COLOURS[kind]
        dash = "" if kind == "req" else ' stroke-dasharray="7 5"'
        tx = min(x1, x2) + 14
        body.append(
            f'<g class="s{i}"><line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{colour}" stroke-width="2.5"{dash} '
            f'marker-end="url(#m{kind})"/>'
            f'<text x="{tx}" y="{y - 8}" class="m" style="fill:{colour}">{escape(label)}</text>'
            + (f'<circle cx="104" cy="{y}" r="11" fill="{colour}"/><text x="104" y="{y + 4}" text-anchor="middle" class="n">{badge}</text>'
               if badge else "")
            + "</g>"
        )
    heads = "".join(
        f'<rect x="{x - 78}" y="92" width="156" height="42" rx="12" fill="{fill}" stroke="{stroke}"/>'
        f'<text x="{x}" y="119" text-anchor="middle" class="who" style="fill:{"#ffffff" if fill == "#7c3aed" else "#0f172a"}">{name}</text>'
        f'<line x1="{x}" y1="134" x2="{x}" y2="{H - 30}" stroke="#e2e8f0" stroke-width="2" stroke-dasharray="2 6"/>'
        for name, x, fill, stroke in LANES
    )
    markers = "".join(
        f'<marker id="m{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M0 0L10 5L0 10z" fill="{c}"/></marker>' for k, c in COLOURS.items()
    )
    legend = (
        '<g transform="translate(640 44)" class="lg">'
        '<line x1="0" y1="0" x2="26" y2="0" stroke="#7c3aed" stroke-width="2.5"/><text x="32" y="4">request</text>'
        '<line x1="104" y1="0" x2="130" y2="0" stroke="#0d9488" stroke-width="2.5" stroke-dasharray="7 5"/><text x="136" y="4">streamed back</text>'
        '<line x1="236" y1="0" x2="262" y2="0" stroke="#d97706" stroke-width="2.5" stroke-dasharray="7 5"/><text x="268" y="4">question</text>'
        "</g>"
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="One request from Kiro through kiro-a2a to a parent agent and its sub-agent, with live progress, a question for you, your answer, and the final result">
<defs>{markers}
<style>
text{{font-family:'Segoe UI',Inter,-apple-system,Helvetica,Arial,sans-serif;fill:#0f172a}}
.h1{{font-size:24px;font-weight:700}} .sub{{font-size:14px;fill:#64748b}} .who{{font-size:14.5px;font-weight:700}}
.m{{font-size:12.5px;font-weight:600}} .n{{font-size:11px;font-weight:700;fill:#ffffff}} .lg text{{font-size:12px;fill:#475569}}
{"".join(styles)}
</style></defs>
<rect width="{W}" height="{H}" rx="22" fill="#ffffff"/><rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="21" fill="none" stroke="#e2e8f0"/>
<text x="40" y="50" class="h1">One request, end to end</text>
<text x="40" y="74" class="sub">You ask in Kiro; the parent agent delegates; you see progress, answer its question, and get the result.</text>
{legend}{heads}{"".join(body)}
</svg>
"""


if __name__ == "__main__":
    out = Path(__file__).with_name("flow.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out}")
