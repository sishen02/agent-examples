"""CockroachDB MCP server for agentic database operations."""

import json
import logging
import os
import sys
from typing import Any

import uvicorn
from fastmcp import FastMCP
from pydantic_settings import BaseSettings

try:
    from cockroachdb_tool.operations import CockroachOperations
    from cockroachdb_tool.providers import (
        CockroachSQLProvider,
        KubernetesAPIProvider,
        NullCockroachProvider,
        NullKubernetesProvider,
    )
except ImportError:
    from operations import CockroachOperations
    from providers import CockroachSQLProvider, KubernetesAPIProvider, NullCockroachProvider, NullKubernetesProvider

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    stream=sys.stdout,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

mcp = FastMCP("CockroachDB Operator")


class ToolSettings(BaseSettings):
    cockroach_dsn: str = "postgresql://root@cockroachdb.cockroachdb.svc.cluster.local:26257/defaultdb?sslmode=disable"
    connect_timeout: int = 10
    k8s_namespace: str = "cockroachdb"
    cockroach_label_selector: str = "app.kubernetes.io/name=cockroachdb"
    statefulset_name: str = "cockroachdb"
    cockroach_container_name: str = "cockroachdb"
    backup_destination: str = "nodelocal://1/cockroachdb-tool"
    grpc_port: int = 26357
    http_port: int = 8080
    secure: bool = False
    enable_kubernetes: bool = True
    mcp_read_only: bool = False
    host: str = "0.0.0.0"
    port: int = 9090
    mcp_transport: str = "http"


settings = ToolSettings()


def _json(data: dict[str, Any] | list[Any]) -> str:
    return json.dumps(data, indent=2, default=str)


def _build_cockroach_provider():
    if not settings.cockroach_dsn:
        return NullCockroachProvider()
    return CockroachSQLProvider(
        settings.cockroach_dsn,
        connect_timeout=settings.connect_timeout,
        backup_destination=settings.backup_destination,
    )


def _build_kubernetes_provider():
    if not settings.enable_kubernetes:
        return NullKubernetesProvider("Kubernetes provider is disabled")
    try:
        return KubernetesAPIProvider(settings.k8s_namespace, settings.cockroach_label_selector)
    except Exception as exc:
        logger.warning("Kubernetes provider unavailable: %s", exc)
        return NullKubernetesProvider(str(exc))


cockroach_provider = _build_cockroach_provider()
kubernetes_provider = _build_kubernetes_provider()


def _operations() -> CockroachOperations:
    return CockroachOperations(
        cockroach_provider,
        kubernetes_provider,
        statefulset_name=settings.statefulset_name,
        container_name=settings.cockroach_container_name,
        grpc_port=settings.grpc_port,
        secure=settings.secure,
        read_only=settings.mcp_read_only,
    )


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_cluster_status(namespace: str | None = None, cluster: str | None = None) -> str:
    """Return typed CockroachDB cluster health and readiness state."""
    try:
        return _json(_operations().get_cluster_status(namespace or settings.k8s_namespace, cluster or settings.statefulset_name))
    except Exception as exc:
        logger.exception("get_cluster_status failed")
        return _json({"error": str(exc)})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def list_database_nodes(namespace: str | None = None, cluster: str | None = None) -> str:
    """Return typed CockroachDB node and pod state."""
    try:
        return _json(_operations().list_database_nodes(namespace or settings.k8s_namespace, cluster or settings.statefulset_name))
    except Exception as exc:
        logger.exception("list_database_nodes failed")
        return _json({"error": str(exc), "nodes": []})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_node_status(node_id: int, namespace: str | None = None, cluster: str | None = None) -> str:
    """Return typed status for one CockroachDB node."""
    try:
        return _json(_operations().get_node_status(namespace or settings.k8s_namespace, cluster or settings.statefulset_name, node_id))
    except Exception as exc:
        logger.exception("get_node_status failed")
        return _json({"error": str(exc), "node_id": node_id})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_storage_status(namespace: str | None = None, cluster: str | None = None) -> str:
    """Return typed PVC/storage state relevant to CockroachDB operations."""
    try:
        return _json(_operations().get_storage_status(namespace or settings.k8s_namespace, cluster or settings.statefulset_name))
    except Exception as exc:
        logger.exception("get_storage_status failed")
        return _json({"error": str(exc), "volumes": []})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_backup_status(namespace: str | None = None, cluster: str | None = None) -> str:
    """Return typed backup recency and location state."""
    try:
        return _json(_operations().get_backup_status(namespace or settings.k8s_namespace, cluster or settings.statefulset_name))
    except Exception as exc:
        logger.exception("get_backup_status failed")
        return _json({"error": str(exc)})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def drain_cockroach_node(
    node_id: int,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Start CockroachDB drain/decommission protocol for one node without deleting its pod or PVC."""
    try:
        return _json(
            _operations().drain_cockroach_node(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                node_id,
            )
        )
    except Exception as exc:
        logger.exception("drain_cockroach_node failed")
        return _json({"error": str(exc), "changed": False, "node_id": node_id})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
def wait_for_node_ready(
    node_id: int,
    timeout_seconds: int = 300,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Wait until one CockroachDB node is both pod-ready and live."""
    try:
        return _json(
            _operations().wait_for_node_ready(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                node_id,
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception as exc:
        logger.exception("wait_for_node_ready failed")
        return _json({"error": str(exc), "changed": False, "node_id": node_id})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
def wait_for_cluster_healthy(
    timeout_seconds: int = 300,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Wait until cluster status is healthy according to typed state projections."""
    try:
        return _json(
            _operations().wait_for_cluster_healthy(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception as exc:
        logger.exception("wait_for_cluster_healthy failed")
        return _json({"error": str(exc), "changed": False})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def restart_cockroach_node(
    node_id: int,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Restart exactly one CockroachDB node. Does not delete PVCs or change replica count."""
    try:
        return _json(
            _operations().restart_cockroach_node(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                node_id,
            )
        )
    except Exception as exc:
        logger.exception("restart_cockroach_node failed")
        return _json({"error": str(exc), "changed": False, "node_id": node_id})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def delete_cockroach_pod(
    pod_name: str,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Delete one CockroachDB pod by Kubernetes pod name."""
    try:
        return _json(
            _operations().delete_cockroach_pod(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                pod_name,
            )
        )
    except Exception as exc:
        logger.exception("delete_cockroach_pod failed")
        return _json({"error": str(exc), "changed": False, "pod_name": pod_name})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def scale_cockroach_cluster(
    target_replicas: int,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Scale the CockroachDB StatefulSet replica count."""
    try:
        return _json(
            _operations().scale_cockroach_cluster(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                target_replicas,
            )
        )
    except Exception as exc:
        logger.exception("scale_cockroach_cluster failed")
        return _json({"error": str(exc), "changed": False, "target_replicas": target_replicas})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def decommission_cockroach_node(
    node_id: int,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Permanently decommission one CockroachDB node without deleting PVCs."""
    try:
        return _json(
            _operations().decommission_cockroach_node(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                node_id,
            )
        )
    except Exception as exc:
        logger.exception("decommission_cockroach_node failed")
        return _json({"error": str(exc), "changed": False, "node_id": node_id})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def expand_data_volume(
    node_id: int,
    target_size_gib: int,
    namespace: str | None = None,
    cluster: str | None = None,
) -> str:
    """Expand one CockroachDB data PVC upward. Never deletes or recreates PVCs."""
    try:
        return _json(
            _operations().expand_data_volume(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                node_id,
                target_size_gib,
            )
        )
    except Exception as exc:
        logger.exception("expand_data_volume failed")
        return _json({"error": str(exc), "changed": False, "node_id": node_id})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def create_backup(
    namespace: str | None = None,
    cluster: str | None = None,
    backup_scope: str = "cluster",
    database: str | None = None,
) -> str:
    """Create a typed CockroachDB backup operation receipt."""
    try:
        return _json(
            _operations().create_backup(
                namespace or settings.k8s_namespace,
                cluster or settings.statefulset_name,
                backup_scope=backup_scope,
                database=database,
            )
        )
    except Exception as exc:
        logger.exception("create_backup failed")
        return _json({"error": str(exc), "changed": False})


def run_server():
    """Run the CockroachDB MCP server."""
    uvicorn_kwargs = {"host": settings.host, "port": settings.port}
    if settings.mcp_transport == "http":
        app = mcp.http_app()
        uvicorn.run(app, **uvicorn_kwargs)
    else:
        mcp.run(transport=settings.mcp_transport, **uvicorn_kwargs)


if __name__ == "__main__":
    run_server()
