"""Sub-agent: LangGraph text tools, served over A2A on port 9102.

Graph: detect -> transform. Operations: word count, uppercase, lowercase,
reverse, title case, keywords. The text to work on is whatever follows a
colon, or the quoted part of the request.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TypedDict

from common import agent_card, serve
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

PORT = 9102
STOP_WORDS = set("a an and are as at be by for from has in is it of on or that the this to was were will with".split())
OPERATIONS = {
    "count": lambda t: f"{len(t.split())} words, {len(t)} characters",
    "uppercase": str.upper,
    "lowercase": str.lower,
    "reverse": lambda t: t[::-1],
    "title": str.title,
    "keywords": lambda t: ", ".join(w for w, _ in Counter(
        w for w in re.findall(r"[a-z']+", t.lower()) if w not in STOP_WORDS and len(w) > 2).most_common(5)) or "(none)",
}
ALIASES = {"words": "count", "upper": "uppercase", "capital": "uppercase", "lower": "lowercase",
           "backwards": "reverse", "keyword": "keywords"}


class State(TypedDict, total=False):
    request: str
    operation: str
    text: str
    answer: str


def detect(state: State) -> State:
    request = state["request"]
    quoted = re.search(r"[\"“'](.+?)[\"”']", request)
    text = quoted.group(1) if quoted else request.split(":", 1)[1].strip() if ":" in request else request
    lowered = request.lower()
    operation = next((op for op in OPERATIONS if op in lowered), None)
    operation = operation or next((op for word, op in ALIASES.items() if word in lowered), "count")
    get_stream_writer()(f"text-tools: operation '{operation}'")
    return {"operation": operation, "text": text}


def transform(state: State) -> State:
    result = OPERATIONS[state["operation"]](state["text"])
    return {"answer": f"{state['operation']}: {result}"}


builder = StateGraph(State)
builder.add_node("detect", detect)
builder.add_node("transform", transform)
builder.add_edge(START, "detect")
builder.add_edge("detect", "transform")
builder.add_edge("transform", END)
graph = builder.compile(checkpointer=InMemorySaver())

CARD = agent_card(
    "Text Tools", "Counts words, changes case, reverses text and finds keywords.", PORT,
    [{"id": "text", "name": "Text tools", "description": "count, uppercase, lowercase, reverse, title, keywords.",
      "tags": ["text"], "examples": ["uppercase: hello world"]}],
)

if __name__ == "__main__":
    serve(graph, CARD, PORT)
