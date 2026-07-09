"""Semantic CockroachDB operations for the MCP tool surface."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

try:
    from cockroachdb_tool.schemas import (
        BackupStatus,
        ClusterStatus,
        DecommissionResult,
        DrainResult,
        NodeInfo,
        NodeStatus,
        OperationReceipt,
        PodDeleteResult,
        RestartResult,
        ScaleResult,
        StorageStatus,
        VolumeExpansionResult,
        WaitResult,
    )
except ImportError:
    from schemas import (
        BackupStatus,
        ClusterStatus,
        DecommissionResult,
        DrainResult,
        NodeInfo,
        NodeStatus,
        OperationReceipt,
        PodDeleteResult,
        RestartResult,
        ScaleResult,
        StorageStatus,
        VolumeExpansionResult,
        WaitResult,
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
        read_only: bool,
    ):
        self.cockroach = cockroach_provider
        self.kubernetes = kubernetes_provider
        self.statefulset_name = statefulset_name
        self.container_name = container_name
        self.grpc_port = grpc_port
        self.secure = secure
        self.read_only = read_only

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

    def drain_cockroach_node(self, namespace: str, cluster: str, node_id: int) -> dict[str, Any]:
        blocked = self._mutation_block("drain_cockroach_node")
        if blocked:
            return DrainResult(node_id=node_id, **blocked.model_dump()).model_dump()
        state_before = self._cluster_status_model(namespace, cluster).model_dump()
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
        status = "success" if self._provider_ok(result) else "failed"
        return DrainResult(
            operation="drain_cockroach_node",
            status=status,
            changed=bool(result.get("changed", True)) and status == "success",
            message="drain started" if status == "success" else "drain failed",
            node_id=node_id,
            state_before=state_before,
            state_after=self._cluster_status_model(namespace, cluster).model_dump(),
            evidence=result,
        ).model_dump()

    def wait_for_node_ready(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        deadline = time.time() + max(0, min(timeout_seconds, 3600))
        while True:
            node = self._node_info(node_id)
            if node and node.pod_ready and node.cockroach_live:
                return WaitResult(
                    operation="wait_for_node_ready",
                    status="success",
                    changed=False,
                    message="node is ready",
                    evidence={"node": node.model_dump()},
                ).model_dump()
            if time.time() >= deadline:
                return WaitResult(
                    operation="wait_for_node_ready",
                    status="failed",
                    changed=False,
                    message="timed out waiting for node readiness",
                    timed_out=True,
                    evidence={"namespace": namespace, "cluster": cluster, "node_id": node_id},
                ).model_dump()
            time.sleep(1)

    def wait_for_cluster_healthy(
        self,
        namespace: str,
        cluster: str,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        deadline = time.time() + max(0, min(timeout_seconds, 3600))
        while True:
            status = self._cluster_status_model(namespace, cluster)
            if self._cluster_healthy(status):
                return WaitResult(
                    operation="wait_for_cluster_healthy",
                    status="success",
                    changed=False,
                    message="cluster is healthy",
                    evidence=status.model_dump(),
                ).model_dump()
            if time.time() >= deadline:
                return WaitResult(
                    operation="wait_for_cluster_healthy",
                    status="failed",
                    changed=False,
                    message="timed out waiting for cluster health",
                    timed_out=True,
                    evidence=status.model_dump(),
                ).model_dump()
            time.sleep(1)

    def restart_cockroach_node(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
    ) -> dict[str, Any]:
        blocked = self._mutation_block("restart_cockroach_node")
        if blocked:
            return RestartResult(node_id=node_id, **blocked.model_dump()).model_dump()
        pod_name = self._pod_name_for_node(node_id)
        result = self.kubernetes.restart_pod(pod_name)
        status = "success" if self._provider_ok(result) else "failed"
        return RestartResult(
            operation="restart_cockroach_node",
            status=status,
            changed=bool(result.get("changed", False)) and status == "success",
            message="node restart requested" if status == "success" else "node restart failed",
            node_id=node_id,
            state_after=self._cluster_status_model(namespace, cluster).model_dump(),
            evidence=result,
        ).model_dump()

    def delete_cockroach_pod(self, namespace: str, cluster: str, pod_name: str) -> dict[str, Any]:
        blocked = self._mutation_block("delete_cockroach_pod")
        if blocked:
            return PodDeleteResult(pod_name=pod_name, **blocked.model_dump()).model_dump()
        result = self.kubernetes.delete_pod(pod_name)
        status = "success" if self._provider_ok(result) else "failed"
        return PodDeleteResult(
            operation="delete_cockroach_pod",
            status=status,
            changed=bool(result.get("changed", False)) and status == "success",
            message="pod deletion requested" if status == "success" else "pod deletion failed",
            pod_name=pod_name,
            state_after=self._cluster_status_model(namespace, cluster).model_dump(),
            evidence=result,
        ).model_dump()

    def scale_cockroach_cluster(
        self,
        namespace: str,
        cluster: str,
        target_replicas: int,
    ) -> dict[str, Any]:
        blocked = self._mutation_block("scale_cockroach_cluster")
        if blocked:
            return ScaleResult(target_replicas=target_replicas, **blocked.model_dump()).model_dump()
        if target_replicas < 1:
            return ScaleResult(
                operation="scale_cockroach_cluster",
                status="failed",
                changed=False,
                message="target_replicas must be at least 1",
                target_replicas=target_replicas,
            ).model_dump()
        state_before = self._cluster_status_model(namespace, cluster)
        result = self.kubernetes.scale_statefulset(self.statefulset_name, target_replicas)
        status = "success" if self._provider_ok(result) else "failed"
        return ScaleResult(
            operation="scale_cockroach_cluster",
            status=status,
            changed=bool(result.get("changed", False)) and status == "success",
            message="cluster scale requested" if status == "success" else "cluster scale failed",
            target_replicas=target_replicas,
            state_before=state_before.model_dump(),
            state_after=self._cluster_status_model(namespace, cluster).model_dump(),
            evidence=result,
        ).model_dump()

    def decommission_cockroach_node(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
    ) -> dict[str, Any]:
        blocked = self._mutation_block("decommission_cockroach_node")
        if blocked:
            return DecommissionResult(node_id=node_id, **blocked.model_dump()).model_dump()
        state_before = self._cluster_status_model(namespace, cluster)
        result = self.kubernetes.exec_cockroach(
            self._exec_pod_name(),
            self.container_name,
            ["node", "decommission", str(node_id), self._secure_flag(), self._node_rpc_host_flag(namespace)],
        )
        status = "success" if self._provider_ok(result) else "failed"
        return DecommissionResult(
            operation="decommission_cockroach_node",
            status=status,
            changed=bool(result.get("changed", True)) and status == "success",
            message="node decommission requested" if status == "success" else "node decommission failed",
            node_id=node_id,
            state_before=state_before.model_dump(),
            state_after=self._cluster_status_model(namespace, cluster).model_dump(),
            evidence=result,
        ).model_dump()

    def expand_data_volume(
        self,
        namespace: str,
        cluster: str,
        node_id: int,
        target_size_gib: int,
    ) -> dict[str, Any]:
        blocked = self._mutation_block("expand_data_volume")
        if blocked:
            return VolumeExpansionResult(
                node_id=node_id,
                target_size_gib=target_size_gib,
                **blocked.model_dump(),
            ).model_dump()
        result = self._call_optional("expand_data_volume", node_id, target_size_gib)
        if result is None:
            result = {"error": "provider does not implement expand_data_volume", "changed": False}
        status = "success" if self._provider_ok(result) else "failed"
        return VolumeExpansionResult(
            operation="expand_data_volume",
            status=status,
            changed=bool(result.get("changed", False)) and status == "success",
            message="volume expansion requested" if status == "success" else "volume expansion failed",
            node_id=node_id,
            target_size_gib=target_size_gib,
            state_after=self.get_storage_status(namespace, cluster),
            evidence=result,
        ).model_dump()

    def create_backup(
        self,
        namespace: str,
        cluster: str,
        backup_scope: str = "cluster",
        database: str | None = None,
    ) -> dict[str, Any]:
        blocked = self._mutation_block("create_backup")
        if blocked:
            data = blocked.model_dump()
            data["backup_id"] = None
            return data
        backup_id = f"{cluster}-{uuid4().hex[:8]}"
        result = self._call_optional("create_backup", backup_scope, database, backup_id)
        if result is None:
            result = {"error": "provider does not implement create_backup", "backup_id": backup_id, "changed": False}
        status = "success" if self._provider_ok(result) else "failed"
        return {
            **OperationReceipt(
                operation="create_backup",
                status=status,
                changed=bool(result.get("changed", False)) and status == "success",
                message="backup requested" if status == "success" else "backup failed",
                evidence=result,
            ).model_dump(),
            "backup_id": result.get("backup_id", backup_id) if status == "success" else None,
        }

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

    def _mutation_block(self, operation: str) -> OperationReceipt | None:
        if self.read_only:
            return OperationReceipt(
                operation=operation,
                status="blocked",
                changed=False,
                message=f"{operation} is blocked because MCP_READ_ONLY is enabled",
            )
        return None

    def _safe_call(self, fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = fn(*args, **kwargs)
            return result if isinstance(result, dict) else {"result": result}
        except Exception as exc:
            return {"error": str(exc)}

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
