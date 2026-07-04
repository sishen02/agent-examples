import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def tool_module(monkeypatch):
    monkeypatch.setenv("MCP_READ_ONLY", "true")
    monkeypatch.setenv("REQUIRE_APPROVAL", "true")
    monkeypatch.setenv("ENABLE_KUBERNETES", "false")
    monkeypatch.delenv("COCKROACH_DSN", raising=False)
    module = importlib.import_module("cockroachdb_tool.cockroachdb_tool")
    return importlib.reload(module)


class FakeCockroachProvider:
    def __init__(self):
        self.executed = []

    def query(self, sql, params=None, max_rows=100):
        return {"columns": ["ok"], "rows": [{"ok": True}], "row_count": 1, "truncated": False}

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return {"changed": True, "row_count": 1}

    def cluster_overview(self):
        return {"source": "sql", "nodes": [{"node_id": 1}], "databases": ["defaultdb"]}

    def node_health(self):
        return {"nodes": [{"node_id": 1, "is_live": True}], "range_summary": []}

    def jobs(self, status=None, limit=25):
        return {"jobs": [{"status": status or "running"}]}


class FakeKubernetesProvider:
    def __init__(self):
        self.scaled = []
        self.restarted = []

    def status(self):
        return {"pods": [{"name": "crdb-0"}], "statefulsets": [], "services": [], "events": []}

    def scale_statefulset(self, name, replicas):
        self.scaled.append((name, replicas))
        return {"changed": True, "statefulset": name, "replicas": replicas}

    def restart_pod(self, pod_name):
        self.restarted.append(pod_name)
        return {"changed": True, "pod": pod_name}


def test_read_only_sql_is_allowed(tool_module):
    tool_module.cockroach_provider = FakeCockroachProvider()

    result = json.loads(tool_module.run_sql("SHOW DATABASES"))

    assert result["row_count"] == 1
    assert result["rows"] == [{"ok": True}]


def test_mutating_sql_requires_approval(tool_module):
    tool_module.cockroach_provider = FakeCockroachProvider()

    result = json.loads(tool_module.run_sql("ALTER TABLE foo ADD COLUMN bar STRING", approved=False))

    assert result["changed"] is False
    assert result["approval_required"] is True
    assert "blocked" in result["error"]


def test_mutating_sql_runs_when_not_read_only_and_approved(tool_module, monkeypatch):
    monkeypatch.setattr(tool_module.settings, "mcp_read_only", False)
    monkeypatch.setattr(tool_module.settings, "require_approval", True)
    provider = FakeCockroachProvider()
    tool_module.cockroach_provider = provider

    result = json.loads(tool_module.run_sql("ALTER TABLE foo ADD COLUMN bar STRING", approved=True))

    assert result["changed"] is True
    assert provider.executed == [("ALTER TABLE foo ADD COLUMN bar STRING", None)]


def test_scale_statefulset_requires_approval(tool_module):
    tool_module.kubernetes_provider = FakeKubernetesProvider()

    result = json.loads(tool_module.scale_statefulset("cockroachdb", 3, approved=False))

    assert result["changed"] is False
    assert result["approval_required"] is True


def test_scale_statefulset_runs_when_approved(tool_module, monkeypatch):
    monkeypatch.setattr(tool_module.settings, "mcp_read_only", False)
    monkeypatch.setattr(tool_module.settings, "require_approval", True)
    provider = FakeKubernetesProvider()
    tool_module.kubernetes_provider = provider

    result = json.loads(tool_module.scale_statefulset("cockroachdb", 3, approved=True))

    assert result["changed"] is True
    assert provider.scaled == [("cockroachdb", 3)]


def test_read_only_sql_classifier(tool_module):
    assert tool_module._is_read_only_sql(" select 1")
    assert tool_module._is_read_only_sql("SHOW JOBS")
    assert tool_module._is_read_only_sql("EXPLAIN SELECT 1")
    assert not tool_module._is_read_only_sql("UPDATE foo SET bar = 1")
