"""LangGraph wiring for the CockroachDB operator agent."""

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from cockroachdb_operator_agent.configuration import Configuration

logger = logging.getLogger(__name__)
config = Configuration()


def get_mcpclient() -> MultiServerMCPClient:
    """Create an MCP client for the CockroachDB tool server."""
    return MultiServerMCPClient(
        {
            "cockroachdb": {
                "url": config.MCP_URL,
                "transport": config.MCP_TRANSPORT,
                "timeout": config.MCP_TIMEOUT,
            }
        }
    )


SYSTEM_PROMPT = """You are a CockroachDB operator agent for human SRE/DBA workflows.

Your job is episodic operations: inspect state, diagnose issues, propose safe plans,
and execute approved tool calls. You are not a continuous Kubernetes reconciler.

Operating rules:
1. Diagnose before acting. Gather current CockroachDB and Kubernetes state before recommending fixes.
2. Do not invent cluster state. Base conclusions on tool output or say what is unknown.
3. Treat these as risky operations: arbitrary mutating SQL, backups, restores, scaling, pod restarts,
   node decommissioning, topology changes, resource deletion, and anything affecting availability or data.
4. Before risky operations, present the exact proposed action and ask for explicit approval.
5. Only pass approved=true to a tool when the user has clearly approved that specific action.
6. If a tool reports MCP_READ_ONLY or approval_required, explain that execution is blocked and provide the plan.
7. Prefer bounded diagnostic SQL. Avoid broad scans unless the user requests them and the risk is explained.
8. After an approved change, re-check relevant health signals and summarize the result.
9. For operations from spec.md, prefer the operation-specific tools such as check_sql_connection, probe_metrics_health, discover_node_id, and start_node_decommission.
10. These tools execute operations and return evidence; do not claim that spec preconditions or postconditions are satisfied unless separate evidence proves that.

Return concise, operator-oriented answers with evidence and next steps."""


FINALIZER_PROMPT = """The previous assistant message was empty.

Use the conversation and tool results below to produce the final user-facing answer.
Do not call tools. Be concise, operator-oriented, and base the answer only on the
provided evidence. If the operation requires approval or failed, say that clearly."""


def content_to_text(content: Any) -> str | None:
    """Return non-empty text from common LangChain message content shapes."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "\n".join(part.strip() for part in parts if part.strip())
        return text or None
    return None


def build_finalizer_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Build a no-tools finalization prompt from completed graph messages."""
    if messages:
        newest_message = messages[-1]
        if isinstance(newest_message, AIMessage) and not getattr(newest_message, "tool_calls", None):
            if not content_to_text(newest_message.content):
                messages = messages[:-1]
    return [SystemMessage(content=FINALIZER_PROMPT), *messages]


def route_after_assistant(state: MessagesState) -> str:
    """Route to tools, finalizer, or end after the assistant node."""
    messages = state["messages"]
    if not messages:
        return "finalizer"

    newest_message = messages[-1]
    if isinstance(newest_message, AIMessage):
        if getattr(newest_message, "tool_calls", None):
            return "tools"
        if not content_to_text(newest_message.content):
            return "finalizer"
    return END


async def get_graph(client: MultiServerMCPClient) -> StateGraph:
    """Build the graph used by the A2A executor."""
    llm = ChatOpenAI(
        model=config.LLM_MODEL,
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_API_BASE,
        temperature=0,
    )

    try:
        tools = await client.get_tools()
        logger.info("Loaded CockroachDB MCP tools: %s", [tool.name for tool in tools])
        llm_with_tools = llm.bind_tools(tools)
    except Exception as exc:
        logger.warning("Failed to load CockroachDB MCP tools: %s", exc)
        tools = []
        llm_with_tools = llm

    sys_msg = SystemMessage(content=SYSTEM_PROMPT)

    def assistant(state: MessagesState) -> MessagesState:
        result = llm_with_tools.invoke([sys_msg] + state["messages"])
        return {"messages": [result]}

    async def finalizer(state: MessagesState) -> MessagesState:
        result = await llm.ainvoke(build_finalizer_messages(state["messages"]))
        text = content_to_text(result.content)
        if not text:
            logger.warning(
                "Finalizer also produced no text: message=%r content_type=%s content_repr=%r response_metadata=%s",
                result,
                type(result.content).__name__,
                result.content,
                result.response_metadata,
            )
        return {"messages": [result]}

    builder = StateGraph(MessagesState)
    builder.add_node("assistant", assistant)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("finalizer", finalizer)
    builder.add_edge(START, "assistant")
    builder.add_conditional_edges("assistant", route_after_assistant)
    builder.add_edge("tools", "assistant")
    builder.add_edge("finalizer", END)
    return builder.compile()
