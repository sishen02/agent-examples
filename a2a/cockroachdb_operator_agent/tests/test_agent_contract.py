import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cockroachdb_operator_agent.agent import (
    ConversationHistory,
    _extract_final_text_from_graph_state,
    _extract_final_text_from_graph_update,
    _format_graph_update,
    get_agent_card,
)
from cockroachdb_operator_agent.graph import SYSTEM_PROMPT, build_finalizer_messages, get_mcpclient
from cockroachdb_operator_agent.trajectory import TrajectoryRecorder


def test_agent_card_describes_cockroachdb_operator():
    card = get_agent_card("localhost", 8000)

    assert card.name == "CockroachDB Operator Agent"
    assert card.capabilities.streaming is True
    assert card.skills[0].id == "cockroachdb_operator"
    assert "cockroachdb" in card.skills[0].tags
    assert "SQL" not in card.description
    assert "backup creation" in card.description


def test_system_prompt_defines_operator_rules():
    assert "Diagnose before acting" in SYSTEM_PROMPT
    assert "not a continuous Kubernetes reconciler" in SYSTEM_PROMPT
    assert "available CockroachDB MCP tools" in SYSTEM_PROMPT
    assert "arbitrary SQL" in SYSTEM_PROMPT
    assert "check_sql_connection" not in SYSTEM_PROMPT
    assert "spec.md" not in SYSTEM_PROMPT


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


def test_extract_final_text_from_last_assistant_message():
    state = {
        "messages": [
            HumanMessage(content="Move the data"),
            AIMessage(content="I need current state before running the migration."),
        ]
    }

    assert _extract_final_text_from_graph_state(state) == "I need current state before running the migration."


def test_extract_final_text_ignores_tool_call_messages():
    state = {
        "messages": [
            AIMessage(content="Prior answer"),
            AIMessage(content="", tool_calls=[{"name": "run_sql", "args": {}, "id": "call-1"}]),
        ]
    }

    assert _extract_final_text_from_graph_state(state) is None


def test_extract_final_text_from_streamed_update():
    event = {
        "assistant": {
            "messages": [
                AIMessage(content="The cluster is healthy."),
            ]
        }
    }

    assert _extract_final_text_from_graph_update(event) == "The cluster is healthy."


def test_extract_final_text_from_streamed_update_ignores_tool_call_messages():
    event = {
        "assistant": {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "check_node_health", "args": {}, "id": "call-1"}]),
            ]
        }
    }

    assert _extract_final_text_from_graph_update(event) is None


def test_format_graph_update_emits_only_tool_calls():
    formatted = _format_graph_update(
        {
            "assistant": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "get_cluster_status",
                                "args": {"namespace": "cockroachdb", "cluster": "crdb"},
                                "id": "call-1",
                            }
                        ],
                    )
                ]
            }
        }
    )

    assert formatted == 'get_cluster_status(namespace="cockroachdb", cluster="crdb")\n'


def test_format_graph_update_emits_tool_results_and_ignores_text_updates():
    formatted = _format_graph_update(
        {
            "tools": {
                "messages": [
                    ToolMessage(content='{"status":"success"}', name="get_cluster_status", tool_call_id="call-1")
                ]
            }
        }
    )

    assert formatted == 'get_cluster_status -> "{\\"status\\":\\"success\\"}"\n'
    assert _format_graph_update({"assistant": {"messages": [AIMessage(content="The cluster is healthy.")]}}) == ""


def test_format_graph_update_formats_multiple_calls_without_trimming_args():
    long_value = "x" * 200
    formatted = _format_graph_update(
        {
            "assistant": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "list_database_nodes", "args": {}, "id": "call-1"},
                            {"name": "create_backup", "args": {"database": long_value}, "id": "call-2"},
                        ],
                    )
                ]
            }
        }
    )

    assert formatted.startswith("list_database_nodes()\ncreate_backup(database=")
    assert long_value in formatted
    assert "..." not in formatted
    assert formatted.endswith("\n")


def test_build_finalizer_messages_removes_empty_final_assistant_message():
    messages = [
        HumanMessage(content="Is the cluster healthy?"),
        AIMessage(content="", tool_calls=[]),
    ]

    finalizer_messages = build_finalizer_messages(messages)

    assert isinstance(finalizer_messages[0], SystemMessage)
    assert "previous assistant message was empty" in finalizer_messages[0].content
    assert finalizer_messages[1:] == messages[:1]


def test_build_finalizer_messages_keeps_nonempty_final_assistant_message():
    messages = [
        HumanMessage(content="Is the cluster healthy?"),
        AIMessage(content="The cluster is healthy."),
    ]

    finalizer_messages = build_finalizer_messages(messages)

    assert finalizer_messages[1:] == messages


def test_trajectory_recorder_writes_successful_turn(tmp_path):
    recorder = TrajectoryRecorder(
        output_dir=str(tmp_path),
        task_id="task-1",
        context_id="ctx-1",
        user_input="Inspect the cluster",
        agent_version="test",
    )
    recorder.record_mcp_connection(success=True, tools=["get_cluster_status"])
    recorder.record_graph_update(
        {
            "assistant": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "get_cluster_status",
                                "args": {"namespace": "cockroachdb"},
                                "id": "call-1",
                            }
                        ],
                    )
                ]
            }
        },
        formatted_event='get_cluster_status(namespace="cockroachdb")\n',
    )
    recorder.record_graph_update(
        {
            "tools": {
                "messages": [
                    ToolMessage(content='{"status":"success"}', name="get_cluster_status", tool_call_id="call-1")
                ]
            }
        },
        formatted_event='get_cluster_status -> "{\\"status\\":\\"success\\"}"\n',
    )
    recorder.finish(status="success", final_text="The cluster is healthy.")

    path = recorder.write()
    data = json.loads(path.read_text())

    assert data["metadata"]["status"] == "success"
    assert data["input"]["text"] == "Inspect the cluster"
    assert data["mcp_connection"]["tools"] == ["get_cluster_status"]
    assert data["final"]["text"] == "The cluster is healthy."
    assert [event["type"] for event in data["tool_events"]] == ["tool_call", "tool_result"]
    assert data["tool_events"][0]["args"] == {"namespace": "cockroachdb"}
    assert not list(tmp_path.glob("*.tmp"))


def test_trajectory_recorder_writes_failure_with_fallback_serialization(tmp_path):
    recorder = TrajectoryRecorder(
        output_dir=str(tmp_path),
        task_id="task/with spaces",
        context_id="ctx/with spaces",
        user_input="Inspect the cluster",
        agent_version="test",
    )
    error = RuntimeError("connection refused")
    recorder.record_mcp_connection(success=False, error=error)
    recorder.record_graph_update({"assistant": {"non_serializable": object()}})
    recorder.finish(status="failed", final_text="Request failed.", error=error)

    path = recorder.write()
    data = json.loads(path.read_text())

    assert "/" not in path.name
    assert data["metadata"]["status"] == "failed"
    assert data["mcp_connection"]["error"] == {
        "type": "RuntimeError",
        "message": "connection refused",
    }
    assert data["error"] == {"type": "RuntimeError", "message": "connection refused"}
    assert "object object" in data["events"][0]["raw"]["assistant"]["non_serializable"]
