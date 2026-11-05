# Example: a LangGraph agent team over A2A, used from Kiro and Amazon Q

A parent agent and two sub-agents, each a LangGraph graph, served with the official A2A
Python SDK. The parent talks to its sub-agents over A2A. Kiro (and Amazon Q) talk to any of
them through kiro-a2a-bridge. No LLM key is needed: the graphs are deterministic, so the
example runs anywhere. Swap a node for an LLM call in your own agents.

```
Kiro IDE / Kiro CLI / Amazon Q
        │ MCP
        ▼
  kiro-a2a-bridge ──A2A──▶ orchestrator :9100  (plan → clarify → delegate → combine)
        │                        │ A2A           │ A2A
        └──A2A──▶ calculator :9101 ◀─────────────┘
        └──A2A──▶ text-tools :9102 ◀────────────────────┘
```

| Agent | Graph | Shows |
|---|---|---|
| `orchestrator_agent.py` | plan → clarify → delegate → combine | splitting work across sub-agents, streaming their progress, conversation memory (LangGraph thread = A2A `contextId`), asking a question with `interrupt()` (A2A `input-required`) |
| `calculator_agent.py` | extract → compute → format | a simple sub-agent with progress updates |
| `text_agent.py` | detect → transform | another sub-agent |
| `common.py` | | serving any compiled LangGraph graph as an A2A agent |

## Run it

```bash
pip install "kiro-a2a-bridge" langgraph "a2a-sdk[http-server]" uvicorn
python run_team.py
```

Try it from a terminal:

```bash
kiro-a2a ask http://127.0.0.1:9100 "calculate (12 + 30) * 2 and then uppercase: hello team"
```

## Connect it to Kiro (and Amazon Q)

```bash
kiro-a2a setup-kiro team-orchestrator http://127.0.0.1:9100 --dir ~ --amazon-q
kiro-a2a setup-kiro calculator        http://127.0.0.1:9101 --dir ~ --amazon-q
kiro-a2a setup-kiro text-tools        http://127.0.0.1:9102 --dir ~ --amazon-q
kiro-a2a doctor
python check_kiro_mcp.py              # does what the IDE does, without the IDE
```

In Kiro, open any folder. The Kiro panel lists the three MCP servers as **Connected**. Then ask:

1. *Ask the team orchestrator agent: calculate (12 + 30) * 2 and then uppercase: hello team*
   → `84` and `HELLO TEAM`, worked out by the two sub-agents.
2. *Ask the team orchestrator in the same conversation: what was the last result?*
   → it remembers.
3. *Ask it to calculate the sum, without numbers*. It asks which numbers. Answer *19 + 23*
   → `42`.

In Amazon Q (VS Code), sign in, open the chat, and ask the same questions. The tools are
`ask_team_orchestrator`, `ask_calculator` and `ask_text_tools`.

## Use your own agents

Replace the graphs, keep `common.serve(graph, card, port)`. Anything a node writes with
`get_stream_writer()` shows up as live progress in Kiro. `interrupt(question)` becomes a
question Kiro asks you. The `answer` key of the final state is the reply.
