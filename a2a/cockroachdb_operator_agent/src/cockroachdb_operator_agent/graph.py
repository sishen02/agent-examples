"""LangGraph wiring for the CockroachDB operator agent."""

import logging

from langchain_core.messages import AIMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from cockroachdb_operator_agent.configuration import Configuration

logger = logging.getLogger(__name__)
config = Configuration()


class ExtendedMessagesState(MessagesState):
    final_answer: str = ""


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

Return concise, operator-oriented answers with evidence and next steps."""


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

    def assistant(state: ExtendedMessagesState) -> ExtendedMessagesState:
        result = llm_with_tools.invoke([sys_msg] + state["messages"])
        updated_state = {"messages": state["messages"] + [result]}
        if isinstance(result, AIMessage) and not result.tool_calls:
            updated_state["final_answer"] = result.content
        return updated_state

    builder = StateGraph(ExtendedMessagesState)
    builder.add_node("assistant", assistant)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "assistant")
    builder.add_conditional_edges("assistant", tools_condition)
    builder.add_edge("tools", "assistant")
    return builder.compile()

