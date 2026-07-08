"""A2A server for the CockroachDB operator agent."""

import json
import logging
import os
from collections import defaultdict, deque
from textwrap import dedent
from typing import Any

import uvicorn
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from openinference.instrumentation.langchain import LangChainInstrumentor
from starlette.applications import Starlette

from a2a.helpers import new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from cockroachdb_operator_agent.configuration import Configuration
from cockroachdb_operator_agent.graph import content_to_text, get_graph, get_mcpclient
from cockroachdb_operator_agent.trajectory import TrajectoryRecorder

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

LangChainInstrumentor().instrument()
config = Configuration()


class ConversationHistory:
    """Bounded in-memory chat history keyed by A2A context_id."""

    def __init__(self, max_messages: int):
        self.max_messages = max(2, max_messages)
        self._messages: dict[str, deque[BaseMessage]] = defaultdict(lambda: deque(maxlen=self.max_messages))

    def build_turn_messages(self, context_id: str, user_input: str) -> list[BaseMessage]:
        """Return previous messages plus the current user message."""
        return list(self._messages[context_id]) + [HumanMessage(content=user_input)]

    def record_turn(self, context_id: str, user_input: str, assistant_output: str) -> None:
        """Persist the current user/assistant text turn."""
        history = self._messages[context_id]
        history.append(HumanMessage(content=user_input))
        history.append(AIMessage(content=assistant_output))

    def get_messages(self, context_id: str) -> list[BaseMessage]:
        """Return a copy of stored messages for tests and diagnostics."""
        return list(self._messages[context_id])


conversation_history = ConversationHistory(config.MAX_HISTORY_MESSAGES)


def _extract_final_text_from_graph_state(state: dict[str, Any]) -> str | None:
    """Extract the final assistant text from the completed LangGraph message state."""
    messages = state.get("messages") or []
    if messages:
        newest_message = messages[-1]
        logger.debug(
            "Graph completed with message_count=%d last_message_type=%s last_has_tool_calls=%s",
            len(messages),
            type(newest_message).__name__,
            bool(getattr(newest_message, "tool_calls", None)),
        )
        if isinstance(newest_message, AIMessage) and not getattr(newest_message, "tool_calls", None):
            text = content_to_text(newest_message.content)
            if not text:
                logger.warning(
                    "Final AIMessage had no text: message=%r content_type=%s content_repr=%r response_metadata=%s",
                    newest_message,
                    type(newest_message.content).__name__,
                    newest_message.content,
                    newest_message.response_metadata,
                )
            return text
    else:
        logger.debug("Graph completed with no messages")
    return None


def _format_graph_update(event: dict[str, Any]) -> str:
    """Format tool calls and tool results from a streamed LangGraph update."""
    lines = list(_iter_tool_events(event))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _iter_tool_events(value: Any):
    if isinstance(value, AIMessage):
        for call in getattr(value, "tool_calls", None) or []:
            yield _format_tool_call(call)
        return
    if isinstance(value, ToolMessage):
        yield f"{value.name or 'tool'} -> {_format_arg(value.content)}"
        return
    if isinstance(value, dict):
        if isinstance(value.get("name"), str) and "args" in value:
            yield _format_tool_call(value)
            return
        for item in value.values():
            yield from _iter_tool_events(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            yield from _iter_tool_events(item)


def _format_tool_call(call: dict[str, Any]) -> str:
    name = call.get("name") or "tool"
    args = call.get("args") or {}
    if isinstance(args, dict):
        rendered_args = ", ".join(f"{key}={_format_arg(value)}" for key, value in args.items())
    elif isinstance(args, list | tuple):
        rendered_args = ", ".join(_format_arg(value) for value in args)
    else:
        rendered_args = _format_arg(args)
    return f"{name}({rendered_args})"


def _format_arg(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _extract_final_text_from_graph_update(event: dict[str, Any]) -> str | None:
    """Extract final assistant text from a single LangGraph streamed update."""
    for update in event.values():
        if isinstance(update, dict):
            text = _extract_final_text_from_graph_state(update)
            if text:
                return text
    return None


def get_agent_card(host: str, port: int) -> AgentCard:
    """Return the A2A agent card."""
    capabilities = AgentCapabilities(streaming=True)
    skill = AgentSkill(
        id="cockroachdb_operator",
        name="CockroachDB Operator",
        description="Diagnose and operate CockroachDB clusters through MCP tools.",
        tags=["cockroachdb", "database", "kubernetes", "sre", "dba"],
        examples=[
            "Why is my CockroachDB cluster unhealthy?",
            "Check CockroachDB node and pod health",
            "Inspect the Kubernetes status for this CockroachDB cluster",
            "Plan a safe restart for pod cockroachdb-1",
            "Create a CockroachDB backup",
        ],
    )
    return AgentCard(
        name="CockroachDB Operator Agent",
        description=dedent(
            """\
            Interactive SRE/DBA agent for CockroachDB operations.

            Capabilities:
            - Inspect CockroachDB cluster state, node health, storage, and backup status
            - Inspect Kubernetes pods, StatefulSets, services, and events
            - Produce evidence-based operational plans
            - Execute backup creation, scaling, node restart, node decommission, and volume expansion through MCP tools

            The agent is not a continuous reconciliation controller.
            """
        ),
        supported_interfaces=[
            AgentInterface(
                url=os.getenv("AGENT_ENDPOINT", f"http://{host}:{port}").rstrip("/") + "/",
                protocol_binding="JSONRPC",
            )
        ],
        version=config.AGENT_VERSION,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=[skill],
    )


class A2AEvent:
    """Helper for A2A task status and final result updates."""

    def __init__(self, task_updater: TaskUpdater):
        self.task_updater = task_updater

    async def emit_event(self, message: str, final: bool = False, failed: bool = False) -> None:
        logger.info("Emitting event %s", message)
        if final or failed:
            await self.task_updater.add_artifact([new_text_part(message)])
            if final:
                await self.task_updater.complete()
            if failed:
                await self.task_updater.failed()
            return

        await self.task_updater.update_status(
            TaskState.TASK_STATE_WORKING,
            new_text_message(
                message,
                context_id=self.task_updater.context_id,
                task_id=self.task_updater.task_id,
            ),
        )


class CockroachDBOperatorExecutor(AgentExecutor):
    """A2A executor for CockroachDB operations."""

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)  # type: ignore[arg-type]
            await event_queue.enqueue_event(task)

        task_updater = TaskUpdater(event_queue, task.id, task.context_id)
        event_emitter = A2AEvent(task_updater)

        user_input = context.get_user_input()
        trajectory = TrajectoryRecorder(
            output_dir=config.TRAJECTORY_DIR,
            task_id=task.id,
            context_id=task.context_id,
            user_input=user_input or "",
            agent_version=config.AGENT_VERSION,
            enabled=config.TRAJECTORY_ENABLED,
        )
        if not user_input or not user_input.strip():
            message = "Error: Empty input provided"
            trajectory.finish(status="failed", final_text=message)
            self._write_trajectory(trajectory)
            await event_emitter.emit_event(message, failed=True)
            return

        mcpclient = get_mcpclient()
        final_text = None
        try:
            try:
                tools = await mcpclient.get_tools()
                logger.info("Connected to CockroachDB MCP tools: %s", [tool.name for tool in tools])
                trajectory.record_mcp_connection(success=True, tools=[tool.name for tool in tools])
            except Exception as tool_error:
                trajectory.record_mcp_connection(success=False, error=tool_error)
                await event_emitter.emit_event(
                    f"Warning: Cannot connect to CockroachDB MCP service at {config.MCP_URL}. "
                    f"The agent can explain the intended workflow but cannot inspect live state. Error: {tool_error}",
                )

            graph = await get_graph(mcpclient)
            graph_input = {"messages": conversation_history.build_turn_messages(task.context_id, user_input)}
            async for event in graph.astream(graph_input, stream_mode="updates"):
                formatted_event = _format_graph_update(event)
                trajectory.record_graph_update(event, formatted_event=formatted_event)
                if formatted_event:
                    await event_emitter.emit_event(formatted_event)
                update_final_text = _extract_final_text_from_graph_update(event)
                if update_final_text:
                    final_text = update_final_text
                logger.info("event: %s", event)

            if not final_text:
                logger.warning("Graph completed without a final assistant answer")
                final_text = "Task completed without a final answer."
            conversation_history.record_turn(task.context_id, user_input, final_text)
            trajectory.finish(status="success", final_text=final_text)
            await event_emitter.emit_event(final_text, final=True)
        except Exception as exc:
            logger.exception("CockroachDB operator execution failed")
            final_text = f"Error: Failed to process CockroachDB request. {exc}"
            trajectory.finish(status="failed", final_text=final_text, error=exc)
            await event_emitter.emit_event(final_text, failed=True)
            raise
        finally:
            self._write_trajectory(trajectory)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")

    @staticmethod
    def _write_trajectory(trajectory: TrajectoryRecorder) -> None:
        try:
            path = trajectory.write()
            if path:
                logger.info("Wrote trajectory file %s", path)
        except Exception:
            logger.exception("Failed to write trajectory file")


def run():
    """Run the A2A server."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    agent_card = get_agent_card(host=host, port=port)

    request_handler = DefaultRequestHandler(
        agent_executor=CockroachDBOperatorExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, "/", enable_v0_3_compat=True))
    app = Starlette(routes=routes)
    uvicorn.run(app, host=host, port=port)
