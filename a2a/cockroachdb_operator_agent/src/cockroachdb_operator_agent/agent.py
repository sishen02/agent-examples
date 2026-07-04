"""A2A server for the CockroachDB operator agent."""

import logging
import os
from collections import defaultdict, deque
from textwrap import dedent

import uvicorn
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
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
from cockroachdb_operator_agent.graph import get_graph, get_mcpclient

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


def get_agent_card(host: str, port: int) -> AgentCard:
    """Return the A2A agent card."""
    capabilities = AgentCapabilities(streaming=True)
    skill = AgentSkill(
        id="cockroachdb_operator",
        name="CockroachDB Operator",
        description="Diagnose and operate CockroachDB clusters through approval-gated MCP tools.",
        tags=["cockroachdb", "database", "kubernetes", "sre", "dba"],
        examples=[
            "Why is my CockroachDB cluster unhealthy?",
            "Check CockroachDB node health and recent failed jobs",
            "Inspect the Kubernetes status for this CockroachDB cluster",
            "Plan a safe restart for pod cockroachdb-1",
            "Trigger a backup after I approve it",
        ],
    )
    return AgentCard(
        name="CockroachDB Operator Agent",
        description=dedent(
            """\
            Interactive SRE/DBA agent for CockroachDB operations.

            Capabilities:
            - Inspect CockroachDB cluster state, node health, jobs, and SQL diagnostics
            - Inspect Kubernetes pods, StatefulSets, services, and events
            - Produce evidence-based operational plans
            - Execute approved backups, SQL, scaling, and pod restart actions through MCP tools

            The agent is not a continuous reconciliation controller. Risky operations require explicit approval.
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
    """Helper for streaming A2A task updates."""

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
        if not user_input or not user_input.strip():
            await event_emitter.emit_event("Error: Empty input provided", failed=True)
            return

        mcpclient = get_mcpclient()
        try:
            try:
                tools = await mcpclient.get_tools()
                logger.info("Connected to CockroachDB MCP tools: %s", [tool.name for tool in tools])
            except Exception as tool_error:
                await event_emitter.emit_event(
                    f"Warning: Cannot connect to CockroachDB MCP service at {config.MCP_URL}. "
                    f"The agent can explain the intended workflow but cannot inspect live state. Error: {tool_error}",
                )

            graph = await get_graph(mcpclient)
            output = None
            graph_input = {"messages": conversation_history.build_turn_messages(task.context_id, user_input)}
            async for event in graph.astream(graph_input, stream_mode="updates"):
                output = event
                await event_emitter.emit_event(
                    "\n".join(
                        f"{key}: "
                        f"{str(value)[: config.MAX_EVENT_DISPLAY_LENGTH] + '...' if len(str(value)) > config.MAX_EVENT_DISPLAY_LENGTH else str(value)}"
                        for key, value in event.items()
                    )
                    + "\n"
                )

            final_answer = output.get("assistant", {}).get("final_answer") if output else None
            final_text = str(final_answer or "Task completed without a final answer.")
            conversation_history.record_turn(task.context_id, user_input, final_text)
            await event_emitter.emit_event(final_text, final=True)
        except Exception as exc:
            logger.exception("CockroachDB operator execution failed")
            await event_emitter.emit_event(f"Error: Failed to process CockroachDB request. {exc}", failed=True)
            raise

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")


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
