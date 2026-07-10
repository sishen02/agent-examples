import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def tool_module(monkeypatch):
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

    def node_health(self):
        return {"sql_available": True, "nodes": [{"node_id": 1, "is_live": True}], "range_summary": []}


class FakeKubernetesProvider:
    def __init__(self):
        self.scaled = []
        self.restarted = []
        self.expanded = []
        self.execs = []

    def status(self):
        return {
            "pods": [{"name": "cockroachdb-0", "ready": True, "node_name": "worker-1"}],
            "statefulsets": [{"name": "cockroachdb", "replicas": 1, "ready_replicas": 1}],
            "services": [],
            "events": [],
        }

    def scale_statefulset(self, name, replicas):
        self.scaled.append((name, replicas))
        return {"changed": True, "statefulset": name, "replicas": replicas}

    def restart_pod(self, pod_name):
        self.restarted.append(pod_name)
        return {"changed": True, "pod": pod_name}

    def delete_pod(self, pod_name):
        self.restarted.append(pod_name)
        return {"changed": True, "pod": pod_name}

    def exec_cockroach(self, pod_name, container, args):
        self.execs.append((pod_name, container, args))
        return {"pod": pod_name, "container": container, "command": ["cockroach", *args], "exit_code": 0}

    def metrics_health(self, **kwargs):
        return {"pods": [{"pod": "cockroachdb-0", "ok": True, "ranges_underreplicated": []}], "all_ok": True, "kwargs": kwargs}

    def storage_status(self):
        return {"volumes": [{"node_id": 1, "size_gib": 10, "allows_expansion": True}]}

    def expand_data_volume(self, node_id, target_size_gib):
        self.expanded.append((node_id, target_size_gib))
        return {"changed": True, "node_id": node_id, "target_size_gib": target_size_gib}


def configure_operations(tool_module, cockroach_provider=None, kubernetes_provider=None):
    cockroach_provider = cockroach_provider or FakeCockroachProvider()
    kubernetes_provider = kubernetes_provider or FakeKubernetesProvider()
    tool_module.cockroach_provider = cockroach_provider
    tool_module.kubernetes_provider = kubernetes_provider
    tool_module.operations = tool_module.CockroachOperations(
        cockroach_provider,
        kubernetes_provider,
        statefulset_name=tool_module.settings.statefulset_name,
        container_name=tool_module.settings.cockroach_container_name,
        grpc_port=tool_module.settings.grpc_port,
        secure=tool_module.settings.secure,
    )
    return cockroach_provider, kubernetes_provider


def test_semantic_cluster_status_and_nodes(tool_module):
    configure_operations(tool_module)

    status = json.loads(tool_module.get_cluster_status("cockroachdb", "cockroachdb"))
    nodes = json.loads(tool_module.list_database_nodes("cockroachdb", "cockroachdb"))
    node = json.loads(tool_module.get_node_status(1, "cockroachdb", "cockroachdb"))

    assert status["cluster_phase"] == "Ready"
    assert status["desired_replicas"] == 1
    assert status["ready_replicas"] == 1
    assert status["live_cockroach_nodes"] == 1
    assert nodes == [
        {
            "node_id": 1,
            "pod_name": "cockroachdb-0",
            "kubernetes_node": "worker-1",
            "pod_ready": True,
            "cockroach_live": True,
            "draining": False,
            "decommissioning": False,
            "disk_used_percent": None,
            "version": None,
        }
    ]
    assert node["exists"] is True
    assert node["pod_name"] == "cockroachdb-0"


def test_semantic_restart_uses_node_mapping_and_does_not_scale(tool_module, monkeypatch):
    provider = FakeKubernetesProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.restart_cockroach_node(1, "cockroachdb", "cockroachdb")

    assert result == "Requested restart of CockroachDB node 1 by deleting pod cockroachdb-0."
    assert provider.restarted == ["cockroachdb-0"]
    assert provider.scaled == []


def test_semantic_restart_does_not_block_on_cluster_health(tool_module, monkeypatch):
    class UnhealthyProvider(FakeKubernetesProvider):
        def status(self):
            return {
                "pods": [{"name": "cockroachdb-0", "ready": False, "node_name": "worker-1"}],
                "statefulsets": [{"name": "cockroachdb", "replicas": 1, "ready_replicas": 0}],
                "services": [],
                "events": [],
            }

    provider = UnhealthyProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.restart_cockroach_node(1, "cockroachdb", "cockroachdb")

    assert result == "Requested restart of CockroachDB node 1 by deleting pod cockroachdb-0."
    assert provider.restarted == ["cockroachdb-0"]


def test_delete_cockroach_pod_deletes_named_pod(tool_module, monkeypatch):
    provider = FakeKubernetesProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.delete_cockroach_pod("cockroachdb-2", "cockroachdb", "cockroachdb")

    assert result == "Requested deletion of CockroachDB pod cockroachdb-2."
    assert provider.restarted == ["cockroachdb-2"]


def test_semantic_drain_uses_rpc_host_for_node_decommission(tool_module, monkeypatch):
    provider = FakeKubernetesProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.drain_cockroach_node(1, "cockroachdb", "cockroachdb")

    assert result == "Started CockroachDB drain for node 1 in StatefulSet cockroachdb."
    assert provider.execs == [
        (
            "cockroachdb-0",
            "cockroachdb",
            [
                "node",
                "decommission",
                "1",
                "--wait=none",
                "--insecure",
                f"--host=cockroachdb-0.cockroachdb.cockroachdb.svc.cluster.local:{tool_module.settings.grpc_port}",
            ],
        )
    ]


def test_semantic_drain_does_not_block_when_node_is_absent_from_status(tool_module, monkeypatch):
    provider = FakeKubernetesProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.drain_cockroach_node(2, "cockroachdb", "cockroachdb")

    assert result == "Started CockroachDB drain for node 2 in StatefulSet cockroachdb."
    assert provider.execs[0][2][2] == "2"


def test_semantic_scale_up_uses_statefulset_tool(tool_module, monkeypatch):
    provider = FakeKubernetesProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.scale_cockroach_statefulset(3, "cockroachdb", "cockroachdb")

    assert result == "Requested scaling CockroachDB StatefulSet cockroachdb to 3 replicas."
    assert provider.scaled == [("cockroachdb", 3)]


def test_semantic_scale_down_calls_statefulset_tool(tool_module, monkeypatch):
    class ThreeNodeProvider(FakeKubernetesProvider):
        def status(self):
            return {
                "pods": [
                    {"name": "cockroachdb-0", "ready": True, "node_name": "worker-1"},
                    {"name": "cockroachdb-1", "ready": True, "node_name": "worker-2"},
                    {"name": "cockroachdb-2", "ready": True, "node_name": "worker-3"},
                ],
                "statefulsets": [{"name": "cockroachdb", "replicas": 3, "ready_replicas": 3}],
                "services": [],
                "events": [],
            }

    provider = ThreeNodeProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.scale_cockroach_statefulset(2, "cockroachdb", "cockroachdb")

    assert result == "Requested scaling CockroachDB StatefulSet cockroachdb to 2 replicas."
    assert provider.scaled == [("cockroachdb", 2)]


def test_semantic_decommission_does_not_block_on_cluster_health(tool_module, monkeypatch):
    class UnhealthyProvider(FakeKubernetesProvider):
        def status(self):
            return {
                "pods": [{"name": "cockroachdb-0", "ready": False, "node_name": "worker-1"}],
                "statefulsets": [{"name": "cockroachdb", "replicas": 1, "ready_replicas": 0}],
                "services": [],
                "events": [],
            }

    provider = UnhealthyProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.decommission_cockroach_node(1, "cockroachdb", "cockroachdb")

    assert result == "Requested decommission of CockroachDB node 1."
    assert provider.execs == [
        (
            "cockroachdb-0",
            "cockroachdb",
            [
                "node",
                "decommission",
                "1",
                "--insecure",
                f"--host=cockroachdb-0.cockroachdb.cockroachdb.svc.cluster.local:{tool_module.settings.grpc_port}",
            ],
        )
    ]


def test_semantic_volume_expansion_calls_provider_without_storage_precheck(tool_module, monkeypatch):
    provider = FakeKubernetesProvider()
    configure_operations(tool_module, kubernetes_provider=provider)

    result = tool_module.expand_data_volume(1, 10, "cockroachdb", "cockroachdb")

    assert result == "Requested expansion of CockroachDB node 1 data volume to 10Gi."
    assert provider.expanded == [(1, 10)]


def test_semantic_create_backup_calls_provider(tool_module):
    configure_operations(tool_module)

    result = tool_module.create_backup("cockroachdb", "cockroachdb")

    assert result == "Error: create_backup failed: provider does not implement create_backup"


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


def test_create_backup_uses_autocommit_and_detached():
    from cockroachdb_tool.providers.sql import CockroachSQLProvider

    class Cursor:
        description = []

        def __init__(self):
            self.statement = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            self.statement = statement

        def fetchall(self):
            return []

    class Conn:
        def __init__(self):
            self.autocommit = False
            self.cursor_obj = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def cursor(self):
            return self.cursor_obj

    class Provider(CockroachSQLProvider):
        def __init__(self):
            super().__init__("postgresql://root@example/defaultdb", backup_destination="nodelocal://1/test-backups")
            self.conn = Conn()

        def _connect(self):
            return self.conn

    provider = Provider()

    result = provider.create_backup("cluster", None, "backup-1")

    assert provider.conn.autocommit is True
    statement = str(provider.conn.cursor_obj.statement)
    assert "BACKUP INTO" in statement
    assert "WITH detached" in statement
    assert result["changed"] is True
    assert result["destination"] == "nodelocal://1/test-backups/backup-1"


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


def test_kubernetes_exec_returns_structured_error(monkeypatch):
    from cockroachdb_tool.providers import kubernetes as kubernetes_provider
    from cockroachdb_tool.providers.kubernetes import KubernetesAPIProvider

    class Core:
        def connect_get_namespaced_pod_exec(self, *args, **kwargs):
            raise AssertionError("stream should wrap this method")

    class Provider(KubernetesAPIProvider):
        def __init__(self):
            self.namespace = "cockroachdb"
            self.core = Core()

    def fail_stream(*args, **kwargs):
        raise AttributeError("'NoneType' object has no attribute 'decode'")

    monkeypatch.setattr(kubernetes_provider, "stream", fail_stream)

    result = Provider().exec_cockroach("cockroachdb-0", "cockroachdb", ["node", "decommission", "1"])

    assert result["changed"] is False
    assert result["exit_code"] is None
    assert result["error_type"] == "AttributeError"
    assert "NoneType" in result["error"]
    assert result["command"] == ["cockroach", "node", "decommission", "1"]


def test_kubernetes_exec_failed_mutating_command_reports_unchanged(monkeypatch):
    from cockroachdb_tool.providers import kubernetes as kubernetes_provider
    from cockroachdb_tool.providers.kubernetes import KubernetesAPIProvider

    class Core:
        def connect_get_namespaced_pod_exec(self, *args, **kwargs):
            raise AssertionError("stream should wrap this method")

    class Response:
        returncode = 1

        def is_open(self):
            return False

        def close(self):
            return None

    class Provider(KubernetesAPIProvider):
        def __init__(self):
            self.namespace = "cockroachdb"
            self.core = Core()

    monkeypatch.setattr(kubernetes_provider, "stream", lambda *args, **kwargs: Response())

    result = Provider().exec_cockroach("cockroachdb-0", "cockroachdb", ["node", "decommission", "1"])

    assert result["exit_code"] == 1
    assert result["changed"] is False
