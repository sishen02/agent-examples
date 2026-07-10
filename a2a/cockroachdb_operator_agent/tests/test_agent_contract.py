import json
import re
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
from cockroachdb_operator_agent.graph import SYSTEM_PROMPT, build_finalizer_messages, get_mcpclient, route_after_assistant
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
    assert "available CockroachDB MCP tools" in SYSTEM_PROMPT
    assert "arbitrary SQL" in SYSTEM_PROMPT
    assert "Call at most one tool at a time" in SYSTEM_PROMPT
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
    history.replace_messages(
        "ctx-1",
        [
            HumanMessage(content="Inspect node health"),
            AIMessage(content=""),
            ToolMessage(content='{"ready":true}', name="get_cluster_status", tool_call_id="call-1"),
            AIMessage(content="Node health looks normal."),
        ],
    )

    messages = history.build_turn_messages("ctx-1", "What did you find?")

    assert [type(message) for message in messages] == [HumanMessage, AIMessage, ToolMessage, AIMessage, HumanMessage]
    assert messages[0].content == "Inspect node health"
    assert messages[2].content == '{"ready":true}'
    assert messages[3].content == "Node health looks normal."
    assert messages[4].content == "What did you find?"


def test_conversation_history_is_isolated_by_context():
    history = ConversationHistory(max_messages=10)
    history.replace_messages("ctx-1", [HumanMessage(content="Check jobs"), AIMessage(content="One failed job.")])

    messages = history.build_turn_messages("ctx-2", "What failed?")

    assert len(messages) == 1
    assert messages[0].content == "What failed?"


def test_conversation_history_trims_old_messages():
    history = ConversationHistory(max_messages=4)
    history.replace_messages(
        "ctx-1",
        [
            HumanMessage(content="turn 1"),
            AIMessage(content="answer 1"),
            HumanMessage(content="turn 2"),
            AIMessage(content="answer 2"),
            HumanMessage(content="turn 3"),
            AIMessage(content="answer 3"),
        ],
    )

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


def test_route_after_assistant_rejects_multiple_tool_calls():
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_cluster_status", "args": {}, "id": "call-1"},
                    {"name": "list_database_nodes", "args": {}, "id": "call-2"},
                ],
            )
        ]
    }

    assert route_after_assistant(state) == "finalizer"


def test_trajectory_recorder_writes_successful_turn(tmp_path):
    recorder = TrajectoryRecorder(
        output_dir=str(tmp_path),
        task_id="task-1",
        context_id="ctx-1",
        user_input="Inspect the cluster",
        model_name="openai/gpt-5",
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
    recorder.record_messages(
        [
            HumanMessage(content="Inspect the cluster"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_cluster_status",
                        "args": {"namespace": "cockroachdb"},
                        "id": "call-1",
                    }
                ],
            ),
            ToolMessage(content='{"status":"success"}', name="get_cluster_status", tool_call_id="call-1"),
            AIMessage(content="The cluster is healthy."),
        ]
    )
    recorder.finish(status="success", final_text="The cluster is healthy.")

    path = recorder.write()
    data = json.loads(path.read_text())

    assert re.fullmatch(r"trajectory-openai-gpt-5-\d{8}T\d{12}Z\.json", path.name)
    assert data["metadata"]["model"] == "openai/gpt-5"
    assert data["metadata"]["status"] == "success"
    assert data["input"]["text"] == "Inspect the cluster"
    assert data["mcp_connection"]["tools"] == ["get_cluster_status"]
    assert data["final"]["text"] == "The cluster is healthy."
    assert [message["role"] for message in data["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert data["messages"][1]["tool_calls"][0]["args"] == {"namespace": "cockroachdb"}
    assert data["messages"][2]["name"] == "get_cluster_status"
    assert data["messages"][2]["content"] == '{"status":"success"}'
    assert not list(tmp_path.glob("*.tmp"))


def test_trajectory_recorder_writes_one_file_per_turn(tmp_path):
    first = TrajectoryRecorder(
        output_dir=str(tmp_path),
        task_id="task-1",
        context_id="ctx-1",
        user_input="Inspect the cluster",
        model_name="claude/sonnet 4",
        agent_version="test",
    )
    first.record_messages(
        [
            HumanMessage(content="Inspect the cluster"),
            AIMessage(content="The cluster is healthy."),
        ]
    )
    first.finish(status="success", final_text="The cluster is healthy.")
    first_path = first.write()

    second = TrajectoryRecorder(
        output_dir=str(tmp_path),
        task_id="task-2",
        context_id="ctx-1",
        user_input="What did you find?",
        model_name="claude/sonnet 4",
        agent_version="test",
    )
    second.record_messages(
        [
            HumanMessage(content="Inspect the cluster"),
            AIMessage(content="The cluster is healthy."),
            HumanMessage(content="What did you find?"),
            AIMessage(content="It was healthy."),
        ]
    )
    second.finish(status="success", final_text="It was healthy.")
    second_path = second.write()

    first_data = json.loads(first_path.read_text())
    second_data = json.loads(second_path.read_text())

    assert first_path != second_path
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert first_path.name.startswith("trajectory-claude-sonnet-4-")
    assert second_path.name.startswith("trajectory-claude-sonnet-4-")
    assert first_data["metadata"]["context_id"] == "ctx-1"
    assert first_data["metadata"]["latest_task_id"] == "task-1"
    assert second_data["metadata"]["context_id"] == "ctx-1"
    assert second_data["metadata"]["latest_task_id"] == "task-2"
    assert [turn["task_id"] for turn in first_data["turns"]] == ["task-1"]
    assert [turn["task_id"] for turn in second_data["turns"]] == ["task-2"]
    assert [message["content"] for message in second_data["messages"]] == [
        "Inspect the cluster",
        "The cluster is healthy.",
        "What did you find?",
        "It was healthy.",
    ]


def test_trajectory_recorder_writes_failure_with_fallback_serialization(tmp_path):
    recorder = TrajectoryRecorder(
        output_dir=str(tmp_path),
        task_id="task/with spaces",
        context_id="ctx/with spaces",
        user_input="Inspect the cluster",
        model_name="model/with spaces",
        agent_version="test",
    )
    error = RuntimeError("connection refused")
    recorder.record_mcp_connection(success=False, error=error)
    recorder.record_graph_update({"assistant": {"non_serializable": object()}})
    recorder.finish(status="failed", final_text="Request failed.", error=error)

    path = recorder.write()
    data = json.loads(path.read_text())

    assert "/" not in path.name
    assert path.name.startswith("trajectory-model-with-spaces-")
    assert data["metadata"]["status"] == "failed"
    assert data["mcp_connection"]["error"] == {
        "type": "RuntimeError",
        "message": "connection refused",
    }
    assert data["error"] == {"type": "RuntimeError", "message": "connection refused"}
    assert data["messages"] == [{"role": "user", "content": "Inspect the cluster"}]
    assert "object object" in data["events"][0]["raw"]["assistant"]["non_serializable"]
