"""Serve a LangGraph graph as an A2A agent, with the official A2A Python SDK.

What the executor does with a graph:
  - the A2A contextId is the LangGraph thread_id, so a conversation keeps its state
  - anything a node writes with get_stream_writer() becomes a `working` status update
  - langgraph.types.interrupt(question) pauses the task as `input-required`;
    the caller's next message on that task resumes the graph with Command(resume=...)
  - the final state's "answer" becomes the task's "answer" artifact
"""

from __future__ import annotations

import logging

import uvicorn
from a2a.helpers.proto_helpers import get_message_text, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from langgraph.types import Command
from starlette.applications import Starlette

log = logging.getLogger("team")


class GraphExecutor(AgentExecutor):
    def __init__(self, graph):
        self.graph = graph  # compiled with a checkpointer

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        text = get_message_text(context.message)
        config = {"configurable": {"thread_id": task.context_id}}
        resuming = task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        graph_input = Command(resume=text) if resuming else {"request": text}

        await updater.start_work()
        final, question = None, None
        try:
            async for mode, chunk in self.graph.astream(graph_input, config, stream_mode=["custom", "updates", "values"]):
                if mode == "custom":
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        updater.new_agent_message([new_text_part(str(chunk))]),
                        metadata={"kind": "progress"},
                    )
                elif mode == "updates" and "__interrupt__" in chunk:
                    question = chunk["__interrupt__"][0].value
                elif mode == "values":
                    final = chunk
        except Exception as exc:  # report, don't crash the server
            log.exception("graph failed")
            await updater.failed(updater.new_agent_message([new_text_part(f"Error: {exc}")]))
            return

        if question is not None:
            await updater.requires_input(updater.new_agent_message([new_text_part(str(question))]))
            return
        await updater.add_artifact([new_text_part((final or {}).get("answer", ""))], name="answer")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def agent_card(name: str, description: str, port: int, skills: list[dict]) -> AgentCard:
    return AgentCard(
        name=name,
        description=description,
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=f"http://127.0.0.1:{port}/", protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(**skill) for skill in skills],
    )


def serve(graph, card: AgentCard, port: int) -> None:
    handler = DefaultRequestHandler(agent_executor=GraphExecutor(graph), task_store=InMemoryTaskStore(), agent_card=card)
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True),
    ]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log.info("%s on http://127.0.0.1:%s/", card.name, port)
    uvicorn.run(Starlette(routes=routes), host="127.0.0.1", port=port, log_level="warning")
