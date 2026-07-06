import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage

from cockroachdb_operator_agent.agent import (
    ConversationHistory,
    _extract_final_text_from_graph_event,
    get_agent_card,
)
from cockroachdb_operator_agent.graph import SYSTEM_PROMPT, get_mcpclient


def test_agent_card_describes_cockroachdb_operator():
    card = get_agent_card("localhost", 8000)

    assert card.name == "CockroachDB Operator Agent"
    assert card.capabilities.streaming is True
    assert card.skills[0].id == "cockroachdb_operator"
    assert "cockroachdb" in card.skills[0].tags


def test_system_prompt_requires_approval_for_risky_operations():
    assert "Diagnose before acting" in SYSTEM_PROMPT
    assert "approved=true" in SYSTEM_PROMPT
    assert "not a continuous Kubernetes reconciler" in SYSTEM_PROMPT


def test_mcp_client_uses_cockroachdb_server(monkeypatch):
    monkeypatch.setenv("MCP_URL", "http://example.test/mcp")
    # Configuration is module-level, so patch it directly for this contract test.
    import cockroachdb_operator_agent.graph as graph

    monkeypatch.setattr(graph.config, "MCP_URL", "http://example.test/mcp")
    client = get_mcpclient()

    assert "cockroachdb" in client.connections
    assert client.connections["cockroachdb"]["url"] == "http://example.test/mcp"


def test_conversation_history_replays_prior_turns():
    history = ConversationHistory(max_messages=10)
    history.record_turn("ctx-1", "Inspect node health", "Node health looks normal.")

    messages = history.build_turn_messages("ctx-1", "What did you find?")

    assert [type(message) for message in messages] == [HumanMessage, AIMessage, HumanMessage]
    assert messages[0].content == "Inspect node health"
    assert messages[1].content == "Node health looks normal."
    assert messages[2].content == "What did you find?"


def test_conversation_history_is_isolated_by_context():
    history = ConversationHistory(max_messages=10)
    history.record_turn("ctx-1", "Check jobs", "One failed job.")

    messages = history.build_turn_messages("ctx-2", "What failed?")

    assert len(messages) == 1
    assert messages[0].content == "What failed?"


def test_conversation_history_trims_old_messages():
    history = ConversationHistory(max_messages=4)
    history.record_turn("ctx-1", "turn 1", "answer 1")
    history.record_turn("ctx-1", "turn 2", "answer 2")
    history.record_turn("ctx-1", "turn 3", "answer 3")

    messages = history.get_messages("ctx-1")

    assert [message.content for message in messages] == ["turn 2", "answer 2", "turn 3", "answer 3"]


def test_extract_final_text_from_explicit_final_answer():
    event = {"assistant": {"final_answer": "Done."}}

    assert _extract_final_text_from_graph_event(event) == "Done."


def test_extract_final_text_from_assistant_message_when_final_answer_missing():
    event = {
        "assistant": {
            "messages": [
                HumanMessage(content="Move the data"),
                AIMessage(content="I need approval before running the migration."),
            ]
        }
    }

    assert _extract_final_text_from_graph_event(event) == "I need approval before running the migration."


def test_extract_final_text_ignores_tool_call_messages():
    event = {
        "assistant": {
            "messages": [
                AIMessage(content="Prior answer"),
                AIMessage(content="", tool_calls=[{"name": "run_sql", "args": {}, "id": "call-1"}]),
            ]
        }
    }

    assert _extract_final_text_from_graph_event(event) == "Prior answer"
