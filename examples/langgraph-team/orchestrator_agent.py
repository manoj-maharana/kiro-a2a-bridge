"""Parent agent: a LangGraph orchestrator, served over A2A on port 9100.

It splits a request into parts, sends each part to the right sub-agent over
A2A (calculator on 9101, text tools on 9102) using the official A2A client,
passes their progress on, and combines the answers.

Graph:
    plan -> clarify (asks with interrupt() when a math request has no numbers)
         -> delegate (A2A calls to sub-agents) -> combine

It remembers the conversation per A2A contextId (LangGraph thread), so
"what was the last result?" works as a follow-up.
"""

from __future__ import annotations

import operator
import re
from typing import Annotated, TypedDict

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_stream_response_text, new_text_message
from a2a.types import Role, SendMessageRequest
from common import agent_card, serve
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

PORT = 9100
SUB_AGENTS = {"calculator": "http://127.0.0.1:9101/", "text-tools": "http://127.0.0.1:9102/"}
MATH_WORDS = re.compile(r"\b(calculate|compute|sum|plus|minus|times|multiply|divide|sqrt|math)\b|[\d)]\s*[-+*/^%]\s*[\d(]", re.I)
TEXT_WORDS = re.compile(r"\b(count|words|uppercase|upper|lowercase|lower|reverse|backwards|title|keywords?)\b", re.I)


class State(TypedDict, total=False):
    request: str
    tasks: list[dict]
    results: list[dict]
    answer: str
    history: Annotated[list[str], operator.add]  # kept across turns (per thread)


def plan(state: State) -> State:
    request = state["request"].strip()
    parts = [p.strip() for p in re.split(r"\s+(?:and then|and also|then|;)\s+", request) if p.strip()]
    tasks = []
    for part in parts:
        if TEXT_WORDS.search(part):
            tasks.append({"agent": "text-tools", "request": part})
        elif MATH_WORDS.search(part) or re.search(r"\d", part):
            tasks.append({"agent": "calculator", "request": part})
    get_stream_writer()(f"orchestrator: plan = {[t['agent'] for t in tasks] or 'answer myself'}")
    return {"tasks": tasks}


def clarify(state: State) -> State:
    tasks = []
    for task in state["tasks"]:
        if task["agent"] == "calculator" and not re.search(r"\d", task["request"]):
            # Pause the A2A task (input-required) until the caller answers.
            numbers = interrupt(f"Which numbers should I use for: '{task['request']}'?")
            task = {**task, "request": f"{task['request']} {numbers}"}
        tasks.append(task)
    return {"tasks": tasks}


async def ask_sub_agent(name: str, request: str, writer) -> str:
    async with httpx.AsyncClient(timeout=60) as http:
        client = await create_client(SUB_AGENTS[name], ClientConfig(streaming=True, httpx_client=http))
        message = new_text_message(request, role=Role.ROLE_USER)
        answer = ""
        async for response in client.send_message(SendMessageRequest(message=message)):
            text = get_stream_response_text(response)
            if response.HasField("status_update") and text:
                writer(f"  {text}")  # pass the sub-agent's progress on
            elif response.HasField("artifact_update") or response.HasField("task"):
                answer = text or answer
        await client.close()
        return answer


async def delegate(state: State) -> State:
    writer = get_stream_writer()
    results = []
    for task in state["tasks"]:
        writer(f"orchestrator: asking {task['agent']} over A2A")
        try:
            answer = await ask_sub_agent(task["agent"], task["request"], writer)
        except Exception as exc:
            answer = f"(could not reach {task['agent']}: {exc})"
        results.append({**task, "answer": answer})
    return {"results": results}


def combine(state: State) -> State:
    request = state["request"].lower()
    if not state.get("results"):
        if "last" in request and state.get("history"):
            answer = f"The last result was: {state['history'][-1]}"
        else:
            answer = ("I coordinate two sub-agents. Try: 'calculate (12 + 30) * 2 and then uppercase: hello team', "
                      "or ask 'what was the last result?'.")
        return {"answer": answer, "history": []}
    lines = [f"- {r['agent']}: {r['answer']}" for r in state["results"]]
    return {"answer": "Results from my sub-agents:\n" + "\n".join(lines),
            "history": [r["answer"] for r in state["results"]]}


builder = StateGraph(State)
for name, node in (("plan", plan), ("clarify", clarify), ("delegate", delegate), ("combine", combine)):
    builder.add_node(name, node)
builder.add_edge(START, "plan")
builder.add_edge("plan", "clarify")
builder.add_edge("clarify", "delegate")
builder.add_edge("delegate", "combine")
builder.add_edge("combine", END)
graph = builder.compile(checkpointer=InMemorySaver())

CARD = agent_card(
    "Team Orchestrator",
    "Parent agent that splits a request and delegates parts to its sub-agents (calculator, text tools) over A2A, "
    "then combines their answers. Remembers the conversation.",
    PORT,
    [{"id": "delegate", "name": "Delegate to sub-agents",
      "description": "Math goes to the calculator, text work to text tools; several parts can be joined with 'and then'.",
      "tags": ["orchestration", "math", "text"],
      "examples": ["calculate (12 + 30) * 2 and then uppercase: hello team", "what was the last result?"]}],
)

if __name__ == "__main__":
    serve(graph, CARD, PORT)
