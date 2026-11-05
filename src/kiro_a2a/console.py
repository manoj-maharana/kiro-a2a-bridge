"""Terminal output for `kiro-a2a ask`: colours and symbols when the terminal
supports them, plain ASCII otherwise (pipes, NO_COLOR, old code pages)."""

from __future__ import annotations

import os
import sys

_CODES = {"bold": "1", "dim": "2", "italic": "3", "red": "31", "green": "32", "yellow": "33",
          "blue": "34", "magenta": "35", "cyan": "36"}


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR") or not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if sys.platform == "win32":
        os.system("")  # switches the Windows console into ANSI mode
    return True


def _supports_unicode(stream) -> bool:
    try:
        "✓✗·◆…".encode(getattr(stream, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Console:
    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.color = _supports_color(self.stream)
        unicode = _supports_unicode(self.stream)
        self.sym = {
            "agent": "◆" if unicode else "*", "dot": "·" if unicode else "-", "ok": "✓" if unicode else "+",
            "fail": "✗" if unicode else "x", "think": "…" if unicode else "~", "ask": "?",
        }

    def style(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(f"\033[{_CODES[s]}m" for s in styles) + text + "\033[0m"

    def line(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def header(self, name: str, wire: str, url: str, signed: bool) -> None:
        dot = f" {self.sym['dot']} "
        badge = dot.join([f"A2A {wire}", *(["signed card"] if signed else []), url])
        self.line(f"{self.style(self.sym['agent'], 'magenta')} {self.style(name, 'bold')}  {self.style(badge, 'dim')}")

    def progress(self, text: str, kind: str | None = None) -> None:
        first, *rest = text.splitlines() or [""]
        lowered = first.lower()
        if kind == "thought":
            mark, styles = self.sym["think"], ("dim", "italic")
        elif kind == "plan":
            mark, styles = self.sym["dot"], ("cyan",)
            first = "plan"
            rest = text.splitlines()
        elif lowered.startswith("denied") or lowered.endswith(": failed") or "failed" in lowered.split()[-1:]:
            mark, styles = self.sym["fail"], ("red",)
        elif lowered.startswith("allowed") or lowered.endswith(": completed"):
            mark, styles = self.sym["ok"], ("green",)
        else:
            mark, styles = self.sym["dot"], ("dim",)
        self.line(f"  {self.style(mark, *styles)} {self.style(first, *styles if kind == 'thought' else ())}")
        for extra in rest:
            self.line(f"      {self.style(extra, 'dim')}")

    def summary(self, state: str, seconds: float, metadata: dict) -> None:
        good = state == "completed"
        waiting = state in ("input-required", "auth-required")
        mark = self.sym["ok"] if good else self.sym["ask"] if waiting else self.sym["fail"]
        colour = "green" if good else "yellow" if waiting else "red"
        details = [f"{metadata.get('durationMs', seconds * 1000) / 1000:.1f}s"]
        if metadata.get("toolCalls") is not None:
            details.append(f"{metadata['toolCalls']} tool call{'s' if metadata['toolCalls'] != 1 else ''}")
        if metadata.get("stopReason") and metadata["stopReason"] != "end_turn":
            details.append(metadata["stopReason"])
        self.line()
        dot = f" {self.sym['dot']} "
        self.line(f"{self.style(mark + ' ' + state, colour, 'bold')}{self.style(dot + dot.join(details), 'dim')}")
