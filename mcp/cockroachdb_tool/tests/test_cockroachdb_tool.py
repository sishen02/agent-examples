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

    def sql_connect(self):
        return {"operation": "check_sql_connection", "sql_available": True}

    def get_cluster_setting(self, setting_name):
        return {"setting_name": setting_name, "rows": [{"value": "1"}]}

    def set_cluster_setting(self, setting_name, value):
        self.executed.append(("set_cluster_setting", setting_name, value))
        return {"changed": True, "setting_name": setting_name, "value": value}

    def read_zone_config(self, target_type=None, target_name=None, max_rows=100):
        return {"rows": [{"target_type": target_type, "target_name": target_name}], "row_count": 1}


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

    def exec_cockroach(self, pod_name, container, args):
        return {"pod": pod_name, "container": container, "command": ["cockroach", *args], "exit_code": 0}

    def metrics_health(self, **kwargs):
        return {"pods": [{"pod": "crdb-0", "ok": True}], "all_ok": True, "kwargs": kwargs}

    def start_config(self, pod_name=None, statefulset_name=None):
        return {"pod": pod_name, "statefulset": statefulset_name}

    def membership_evidence(self, statefulset_name, desired_replicas):
        return {"statefulset": statefulset_name, "desired_replicas": desired_replicas}

    def rollout_evidence(self, statefulset_name, partition=None, desired_image=None):
        return {"statefulset": statefulset_name, "partition": partition, "desired_image": desired_image}

    def cleanup_restart_annotation(self, resource_name, annotation_key, expected_value=None):
        return {"changed": True, "resource": resource_name, "annotation_key": annotation_key}

    def sync_ingress_host(self, name, host, service_name, service_port, tls_secret_name=None, path="/"):
        return {"changed": True, "ingress": name, "host": host}

    def delete_ingress(self, name):
        return {"changed": True, "ingress": name, "deleted": True}

    def version_check_job(self, job_name, image, expected_version=None, timeout_seconds=120, delete_after=True):
        return {"changed": True, "job": job_name, "image": image, "expected_version": expected_version}


def test_read_only_sql_is_allowed(tool_module):
    tool_module.cockroach_provider = FakeCockroachProvider()

    result = json.loads(tool_module.run_sql("SHOW DATABASES"))

    assert result["row_count"] == 1
    assert result["rows"] == [{"ok": True}]


def test_settings_allow_mutations_by_default(tool_module, monkeypatch):
    monkeypatch.delenv("MCP_READ_ONLY", raising=False)
    monkeypatch.delenv("REQUIRE_APPROVAL", raising=False)

    settings = tool_module.ToolSettings()

    assert settings.mcp_read_only is False
    assert settings.require_approval is False


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


def test_spec_named_sql_tools(tool_module, monkeypatch):
    provider = FakeCockroachProvider()
    tool_module.cockroach_provider = provider
    monkeypatch.setattr(tool_module.settings, "mcp_read_only", False)
    monkeypatch.setattr(tool_module.settings, "require_approval", True)

    assert json.loads(tool_module.check_sql_connection())["sql_available"] is True
    assert json.loads(tool_module.get_cluster_setting("cluster.organization"))["setting_name"] == "cluster.organization"
    assert json.loads(tool_module.read_zone_config("DATABASE", "defaultdb"))["row_count"] == 1

    blocked = json.loads(tool_module.set_cluster_setting("kv.snapshot_rebalance.max_rate", "1MiB", approved=False))
    assert blocked["approval_required"] is True

    changed = json.loads(tool_module.set_cluster_setting("kv.snapshot_rebalance.max_rate", "1MiB", approved=True))
    assert changed["changed"] is True
    assert provider.executed == [("set_cluster_setting", "kv.snapshot_rebalance.max_rate", "1MiB")]


def test_spec_named_kubernetes_tools(tool_module, monkeypatch):
    provider = FakeKubernetesProvider()
    tool_module.kubernetes_provider = provider
    monkeypatch.setattr(tool_module.settings, "mcp_read_only", False)
    monkeypatch.setattr(tool_module.settings, "require_approval", True)
    monkeypatch.setattr(tool_module.settings, "statefulset_name", "cockroachdb")

    assert json.loads(tool_module.probe_metrics_health())["passes"][0]["all_ok"] is True
    assert json.loads(tool_module.discover_node_id(target_host="cockroachdb-0"))["exit_code"] == 0
    assert json.loads(tool_module.get_start_config())["statefulset"] == "cockroachdb"
    assert json.loads(tool_module.get_membership_evidence(desired_replicas=3))["desired_replicas"] == 3
    assert json.loads(tool_module.get_rollout_evidence(partition=0))["partition"] == 0

    blocked = json.loads(tool_module.start_node_decommission(2, approved=False))
    assert blocked["approval_required"] is True

    changed = json.loads(tool_module.start_node_decommission(2, approved=True))
    assert changed["approved"] is True
    assert changed["exit_code"] == 0


def test_cluster_overview_avoids_internal_tables():
    from cockroachdb_tool.providers.sql import CockroachSQLProvider

    class RecordingProvider(CockroachSQLProvider):
        def __init__(self):
            self.queries = []

        def query(self, sql, params=None, max_rows=100):
            self.queries.append(sql)
            if "version()" in sql:
                return {"rows": [{"version": "CockroachDB test"}]}
            if "SHOW DATABASES" in sql:
                return {"rows": [{"database_name": "defaultdb"}]}
            return {"rows": []}

    provider = RecordingProvider()
    result = provider.cluster_overview()

    assert result["source"] == "sql_safe"
    assert result["nodes"] == []
    assert result["databases"] == ["defaultdb"]
    assert "crdb_internal" not in "\n".join(provider.queries)


def test_node_health_avoids_internal_tables():
    from cockroachdb_tool.providers.sql import CockroachSQLProvider

    class RecordingProvider(CockroachSQLProvider):
        def __init__(self):
            self.queries = []

        def query(self, sql, params=None, max_rows=100):
            self.queries.append(sql)
            if "SELECT 1 AS sql_available" in sql:
                return {"rows": [{"sql_available": 1, "checked_at": "now"}]}
            return {"rows": []}

    provider = RecordingProvider()
    result = provider.node_health()

    assert result["source"] == "sql_safe"
    assert result["sql_available"] is True
    assert result["nodes"] == []
    assert result["range_summary"] == []
    assert "crdb_internal" not in "\n".join(provider.queries)


def test_parse_simple_label_selector():
    from cockroachdb_tool.providers.kubernetes import _parse_simple_label_selector

    assert _parse_simple_label_selector("app.kubernetes.io/name=cockroachdb") == {
        "app.kubernetes.io/name": "cockroachdb"
    }
    assert _parse_simple_label_selector("app==cockroachdb,tier=database") == {
        "app": "cockroachdb",
        "tier": "database",
    }
    assert _parse_simple_label_selector("app!=cockroachdb") == {}


def test_kubernetes_status_falls_back_to_legacy_label_and_matches_service_selector():
    from cockroachdb_tool.providers.kubernetes import KubernetesAPIProvider

    class Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def klist(items):
        return Obj(items=items)

    pod = Obj(
        metadata=Obj(name="cockroachdb-abc", creation_timestamp=None),
        status=Obj(phase="Running", container_statuses=[Obj(ready=True, restart_count=0)]),
        spec=Obj(node_name="node-1"),
    )
    service = Obj(
        metadata=Obj(name="cockroachdb", labels={}),
        spec=Obj(type="ClusterIP", cluster_ip="10.0.0.1", ports=[Obj(port=26257)], selector={"app": "cockroachdb"}),
    )

    class Core:
        def list_namespaced_pod(self, namespace, label_selector):
            assert namespace == "cockroachdb"
            return klist([pod] if label_selector == "app=cockroachdb" else [])

        def list_namespaced_service(self, namespace, label_selector=None):
            assert namespace == "cockroachdb"
            if label_selector is None:
                return klist([service])
            return klist([])

        def list_namespaced_event(self, namespace, limit=25):
            assert namespace == "cockroachdb"
            assert limit == 25
            return klist([])

    class Apps:
        def list_namespaced_stateful_set(self, namespace, label_selector):
            assert namespace == "cockroachdb"
            return klist([])

    provider = object.__new__(KubernetesAPIProvider)
    provider.namespace = "cockroachdb"
    provider.label_selector = "app.kubernetes.io/name=cockroachdb"
    provider.core = Core()
    provider.apps = Apps()

    status = provider.status()

    assert status["configured_label_selector"] == "app.kubernetes.io/name=cockroachdb"
    assert status["label_selector"] == "app=cockroachdb"
    assert status["pods"][0]["name"] == "cockroachdb-abc"
    assert status["services"][0]["name"] == "cockroachdb"
