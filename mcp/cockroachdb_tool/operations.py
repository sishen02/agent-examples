"""Semantic CockroachDB operations for the MCP tool surface."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

try:
    from cockroachdb_tool.schemas import (
        BackupStatus,
        ClusterStatus,
        NodeInfo,
        NodeStatus,
        StorageStatus,
    )
except ImportError:
    from schemas import (
        BackupStatus,
        ClusterStatus,
        NodeInfo,
        NodeStatus,
        StorageStatus,
    )


class CockroachOperations:
    """Typed database-operation layer over SQL and Kubernetes providers."""

    def __init__(
        self,
        cockroach_provider: Any,
        kubernetes_provider: Any,
        *,
        statefulset_name: str,
        container_name: str,
        grpc_port: int,
        secure: bool,
    ):
        self.cockroach = cockroach_provider
        self.kubernetes = kubernetes_provider
        self.statefulset_name = statefulset_name
        self.container_name = container_name
        self.grpc_port = grpc_port
        self.secure = secure

    def get_cluster_status(self, namespace: str, cluster: str) -> dict[str, Any]:
        status = self._cluster_status_model(namespace, cluster)
        return status.model_dump()

    def list_database_nodes(self, namespace: str, cluster: str) -> list[dict[str, Any]]:
        return [node.model_dump() for node in self._node_infos()]

    def get_node_status(self, namespace: str, cluster: str, node_id: int) -> dict[str, Any]:
        node = self._node_info(node_id)
        if node is None:
            pod_name = self._pod_name_for_node(node_id)
            return NodeStatus(
                namespace=namespace,
                cluster=cluster,
                node_id=node_id,
                pod_name=pod_name,
                exists=False,
                evidence={"reason": "node was not found in Kubernetes or SQL status"},
            ).model_dump()
        return NodeStatus(namespace=namespace, cluster=cluster, **node.model_dump()).model_dump()

    def get_storage_status(self, namespace: str, cluster: str) -> dict[str, Any]:
        provider_result = self._call_optional("storage_status")
        volumes = []
        if isinstance(provider_result, dict):
            volumes = provider_result.get("volumes") or provider_result.get("pvcs") or []
        return StorageStatus(
            namespace=namespace,
            cluster=cluster,
            volumes=volumes,
            evidence=provider_result if isinstance(provider_result, dict) else {},
        ).model_dump()

    def get_backup_status(self, namespace: str, cluster: str) -> dict[str, Any]:
        provider_result = self._call_optional("backup_status")
        if not isinstance(provider_result, dict):
            provider_result = {}
        return BackupStatus(
            namespace=namespace,
            cluster=cluster,
            latest_successful_backup_time=provider_result.get("latest_successful_backup_time"),
            backup_location=provider_result.get("backup_location"),
            recent_enough=bool(provider_result.get("recent_enough", False)),
            evidence=provider_result,
        ).model_dump()

    def drain_cockroach_node(self, namespace: str, cluster: str, node_id: int) -> str:
        result = self.kubernetes.exec_cockroach(
            self._exec_pod_name(),
            self.container_name,
            [
                "node",
                "decommission",
                str(node_id),
                "--wait=none",
                self._secure_flag(),
                self._node_rpc_host_flag(namespace),
            ],
        )
        if not self._provider_ok(result):
            return self._error_message("drain_cockroach_node", "drain failed", result)
        return f"Started CockroachDB drain for node {node_id} in StatefulSet {cluster}."

    def wait_for_node_ready(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
        timeout_seconds: int = 300,
    ) -> str:
        deadline = time.time() + max(0, min(timeout_seconds, 3600))
        while True:
            node = self._node_info(node_id)
            if node and node.pod_ready and node.cockroach_live:
                return f"CockroachDB node {node_id} is ready."
            if time.time() >= deadline:
                return f"Error: timed out waiting for CockroachDB node {node_id} to become ready."
            time.sleep(1)

    def wait_for_cluster_healthy(
        self,
        namespace: str,
        cluster: str,
        timeout_seconds: int = 300,
    ) -> str:
        deadline = time.time() + max(0, min(timeout_seconds, 3600))
        while True:
            status = self._cluster_status_model(namespace, cluster)
            if self._cluster_healthy(status):
                return f"CockroachDB StatefulSet {cluster} is healthy."
            if time.time() >= deadline:
                return f"Error: timed out waiting for CockroachDB StatefulSet {cluster} to become healthy."
            time.sleep(1)

    def restart_cockroach_node(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
    ) -> str:
        pod_name = self._pod_name_for_node(node_id)
        result = self.kubernetes.restart_pod(pod_name)
        if not self._provider_ok(result):
            return self._error_message("restart_cockroach_node", "node restart failed", result)
        return f"Requested restart of CockroachDB node {node_id} by deleting pod {pod_name}."

    def delete_cockroach_pod(self, namespace: str, cluster: str, pod_name: str) -> str:
        result = self.kubernetes.delete_pod(pod_name)
        if not self._provider_ok(result):
            return self._error_message("delete_cockroach_pod", "pod deletion failed", result)
        return f"Requested deletion of CockroachDB pod {pod_name}."

    def scale_cockroach_statefulset(
        self,
        namespace: str,
        cluster: str,
        target_replicas: int,
    ) -> str:
        if target_replicas < 1:
            return "Error: target_replicas must be at least 1."
        result = self.kubernetes.scale_statefulset(self.statefulset_name, target_replicas)
        if not self._provider_ok(result):
            return self._error_message("scale_cockroach_statefulset", "StatefulSet scale failed", result)
        return f"Requested scaling CockroachDB StatefulSet {self.statefulset_name} to {target_replicas} replicas."

    def decommission_cockroach_node(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
    ) -> str:
        result = self.kubernetes.exec_cockroach(
            self._exec_pod_name(),
            self.container_name,
            ["node", "decommission", str(node_id), self._secure_flag(), self._node_rpc_host_flag(namespace)],
        )
        if not self._provider_ok(result):
            return self._error_message("decommission_cockroach_node", "node decommission failed", result)
        return f"Requested decommission of CockroachDB node {node_id}."

    def expand_data_volume(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
        target_size_gib: int,
    ) -> str:
        result = self._call_optional("expand_data_volume", node_id, target_size_gib)
        if result is None:
            result = {"error": "provider does not implement expand_data_volume", "changed": False}
        if not self._provider_ok(result):
            return self._error_message("expand_data_volume", "volume expansion failed", result)
        return f"Requested expansion of CockroachDB node {node_id} data volume to {target_size_gib}Gi."

    def create_backup(
        self,
        namespace: str,
        cluster: str,
        backup_scope: str = "cluster",
        database: str | None = None,
    ) -> str:
        backup_id = f"{cluster}-{uuid4().hex[:8]}"
        result = self._call_optional("create_backup", backup_scope, database, backup_id)
        if result is None:
            result = {"error": "provider does not implement create_backup", "backup_id": backup_id, "changed": False}
        if not self._provider_ok(result):
            return self._error_message("create_backup", "backup failed", result)
        return f"Requested CockroachDB {backup_scope} backup {result.get('backup_id', backup_id)}."

    def _cluster_status_model(self, namespace: str, cluster: str) -> ClusterStatus:
        k8s_status = self._safe_call(self.kubernetes.status)
        sql_status = self._safe_call(self.cockroach.node_health)
        sql_ready = not bool(sql_status.get("error")) and bool(sql_status.get("sql_available", True))
        statefulsets = k8s_status.get("statefulsets", []) if isinstance(k8s_status, dict) else []
        pods = k8s_status.get("pods", []) if isinstance(k8s_status, dict) else []
        primary_sts = self._pick_statefulset(statefulsets)
        desired = int(primary_sts.get("replicas") or len(pods) or 0) if primary_sts else len(pods)
        ready = int(primary_sts.get("ready_replicas") or sum(1 for pod in pods if pod.get("ready")))
        nodes = self._node_infos(k8s_status=k8s_status, sql_status=sql_status)
        live_nodes = sum(1 for node in nodes if node.cockroach_live)
        under_replicated = self._under_replicated_ranges()
        healthy = desired > 0 and ready >= desired and sql_ready and under_replicated == 0
        phase = "Ready" if healthy else "Degraded" if desired and ready < desired else "Unknown"
        return ClusterStatus(
            namespace=namespace,
            cluster=cluster,
            cluster_phase=phase,
            desired_replicas=desired,
            ready_replicas=ready,
            live_cockroach_nodes=live_nodes,
            unavailable_ranges=0,
            under_replicated_ranges=under_replicated,
            sql_ready=sql_ready,
            operator_ready=not bool(k8s_status.get("error")) if isinstance(k8s_status, dict) else False,
            evidence={"kubernetes": k8s_status, "sql": sql_status},
        )

    def _node_infos(
        self,
        *,
        k8s_status: dict[str, Any] | None = None,
        sql_status: dict[str, Any] | None = None,
    ) -> list[NodeInfo]:
        k8s_status = k8s_status if k8s_status is not None else self._safe_call(self.kubernetes.status)
        sql_status = sql_status if sql_status is not None else self._safe_call(self.cockroach.node_health)
        sql_nodes = sql_status.get("nodes", []) if isinstance(sql_status, dict) else []
        pods = k8s_status.get("pods", []) if isinstance(k8s_status, dict) else []
        infos: list[NodeInfo] = []
        for index, pod in enumerate(pods, start=1):
            node_id = self._node_id_from_pod(pod.get("name"), default=index)
            sql_node = self._matching_sql_node(sql_nodes, node_id)
            infos.append(
                NodeInfo(
                    node_id=node_id,
                    pod_name=pod.get("name") or self._pod_name_for_node(node_id),
                    kubernetes_node=pod.get("node_name"),
                    pod_ready=bool(pod.get("ready")),
                    cockroach_live=bool(sql_node.get("is_live", pod.get("ready", False))),
                    draining=bool(sql_node.get("draining", False)),
                    decommissioning=bool(sql_node.get("decommissioning", False)),
                    disk_used_percent=sql_node.get("disk_used_percent"),
                    version=sql_node.get("version"),
                )
            )
        return infos

    def _node_info(self, node_id: int) -> NodeInfo | None:
        for node in self._node_infos():
            if node.node_id == node_id:
                return node
        return None

    def _safe_call(self, fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = fn(*args, **kwargs)
            return result if isinstance(result, dict) else {"result": result}
        except Exception as exc:
            return {"error": str(exc)}

    def _error_message(self, operation: str, fallback: str, result: Any) -> str:
        if isinstance(result, dict):
            detail = result.get("error") or result.get("message") or result.get("stderr") or fallback
        else:
            detail = result or fallback
        return f"Error: {operation} failed: {detail}"

    def _call_optional(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self.kubernetes, name, None)
        if callable(fn):
            return fn(*args, **kwargs)
        fn = getattr(self.cockroach, name, None)
        if callable(fn):
            return fn(*args, **kwargs)
        return None

    def _pick_statefulset(self, statefulsets: list[dict[str, Any]]) -> dict[str, Any] | None:
        for sts in statefulsets:
            if sts.get("name") == self.statefulset_name:
                return sts
        return statefulsets[0] if statefulsets else None

    def _under_replicated_ranges(self) -> int:
        result = self._safe_call(
            self.kubernetes.metrics_health,
            statefulset_name=self.statefulset_name,
            http_port=8080,
            secure=self.secure,
        )
        total = 0
        for pod in result.get("pods", []):
            for sample in pod.get("ranges_underreplicated", []):
                try:
                    total += int(float(sample.get("value", 0)))
                except (TypeError, ValueError):
                    continue
        return total

    def _matching_sql_node(self, nodes: list[dict[str, Any]], node_id: int) -> dict[str, Any]:
        for node in nodes:
            if node.get("node_id") == node_id:
                return node
        return {}

    def _node_id_from_pod(self, pod_name: str | None, default: int) -> int:
        if pod_name:
            suffix = pod_name.rsplit("-", 1)[-1]
            if suffix.isdigit():
                return int(suffix) + 1
        return default

    def _pod_name_for_node(self, node_id: int) -> str:
        return f"{self.statefulset_name}-{max(0, node_id - 1)}"

    def _exec_pod_name(self) -> str:
        return f"{self.statefulset_name}-0"

    def _node_rpc_host_flag(self, namespace: str) -> str:
        host = f"{self._exec_pod_name()}.{self.statefulset_name}.{namespace}.svc.cluster.local:{self.grpc_port}"
        return f"--host={host}"

    def _secure_flag(self) -> str:
        return "--secure" if self.secure else "--insecure"

    def _provider_ok(self, result: Any) -> bool:
        return isinstance(result, dict) and not result.get("error") and result.get("exit_code", 0) == 0

    def _cluster_healthy(self, status: ClusterStatus) -> bool:
        return (
            status.desired_replicas > 0
            and status.ready_replicas >= status.desired_replicas
            and status.sql_ready
            and status.under_replicated_ranges == 0
        )
