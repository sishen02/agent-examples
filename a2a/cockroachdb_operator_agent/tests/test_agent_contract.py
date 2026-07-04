import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cockroachdb_operator_agent.agent import get_agent_card
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

