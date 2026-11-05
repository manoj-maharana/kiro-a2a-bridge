"""Sub-agent: a LangGraph calculator, served over A2A on port 9101.

Graph: extract -> compute -> format. No LLM needed; arithmetic is evaluated
safely (numbers, + - * / // % **, parentheses, sqrt, round, abs, min, max).
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import TypedDict

from common import agent_card, serve
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

PORT = 9101
OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
             ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
             ast.USub: operator.neg, ast.UAdd: operator.pos}
FUNCTIONS = {"sqrt": math.sqrt, "round": round, "abs": abs, "min": min, "max": max}


def safe_eval(expression: str) -> float:
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            if isinstance(node.op, ast.Pow) and abs(walk(node.right)) > 100:
                raise ValueError("exponent too large")
            return OPERATORS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](walk(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS:
            return FUNCTIONS[node.func.id](*[walk(a) for a in node.args])
        raise ValueError("only arithmetic is allowed")

    return walk(ast.parse(expression, mode="eval"))


class State(TypedDict, total=False):
    request: str
    expression: str
    result: str
    answer: str


def extract(state: State) -> State:
    text = state["request"].replace("^", "**").replace("×", "*").replace("÷", "/")
    candidates = re.findall(r"(?:sqrt|round|abs|min|max|[\d(.])[\d\s+\-*/%().,a-z]*", text)
    expression = max(candidates, key=len, default="").strip(" .,")
    get_stream_writer()(f"calculator: expression is '{expression}'")
    return {"expression": expression}


def compute(state: State) -> State:
    try:
        value = safe_eval(state["expression"])
        value = int(value) if isinstance(value, float) and value.is_integer() else round(value, 10)
        get_stream_writer()("calculator: computed")
        return {"result": str(value)}
    except Exception as exc:
        return {"result": f"error ({exc})"}


def format_answer(state: State) -> State:
    return {"answer": f"{state['expression']} = {state['result']}"}


builder = StateGraph(State)
builder.add_node("extract", extract)
builder.add_node("compute", compute)
builder.add_node("format", format_answer)
builder.add_edge(START, "extract")
builder.add_edge("extract", "compute")
builder.add_edge("compute", "format")
builder.add_edge("format", END)
graph = builder.compile(checkpointer=InMemorySaver())

CARD = agent_card(
    "Calculator", "Evaluates arithmetic expressions exactly.", PORT,
    [{"id": "calculate", "name": "Calculate", "description": "Arithmetic with + - * / ** %, sqrt, round, min, max.",
      "tags": ["math"], "examples": ["what is (12 + 30) * 2"]}],
)

if __name__ == "__main__":
    serve(graph, CARD, PORT)
