"""CockroachDB MCP server for agentic database operations."""

import json
import logging
import os
import re
import sys
import time
from typing import Any
from uuid import uuid4

import uvicorn
from fastmcp import FastMCP
from pydantic_settings import BaseSettings

try:
    from cockroachdb_tool.providers import (
        CockroachSQLProvider,
        KubernetesAPIProvider,
        NullCockroachProvider,
        NullKubernetesProvider,
    )
except ImportError:
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
    grpc_port: int = 26257
    http_port: int = 8080
    secure: bool = False
    enable_kubernetes: bool = True
    mcp_read_only: bool = False
    require_approval: bool = False
    host: str = "0.0.0.0"
    port: int = 9090
    mcp_transport: str = "http"


settings = ToolSettings()


def _json(data: dict[str, Any] | list[Any]) -> str:
    return json.dumps(data, indent=2, default=str)


def _build_cockroach_provider():
    if not settings.cockroach_dsn:
        return NullCockroachProvider()
    return CockroachSQLProvider(settings.cockroach_dsn, connect_timeout=settings.connect_timeout)


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


_READ_ONLY_SQL = re.compile(r"^\s*(select|show|explain|with)\b", re.IGNORECASE | re.DOTALL)


def _is_read_only_sql(sql: str) -> bool:
    return bool(_READ_ONLY_SQL.match(sql.strip()))


def _approval_error(operation: str) -> dict[str, Any] | None:
    if settings.mcp_read_only:
        return {
            "error": f"{operation} is blocked because MCP_READ_ONLY is enabled",
            "changed": False,
            "approval_required": True,
        }
    if settings.require_approval:
        return {
            "error": f"{operation} requires explicit approval",
            "changed": False,
            "approval_required": True,
        }
    return None


def _require_approval(operation: str, approved: bool) -> dict[str, Any] | None:
    if settings.mcp_read_only:
        return {
            "error": f"{operation} is blocked because MCP_READ_ONLY is enabled",
            "changed": False,
            "approval_required": True,
        }
    if settings.require_approval and not approved:
        return {
            "error": f"{operation} requires explicit approval",
            "changed": False,
            "approval_required": True,
        }
    return None


def _secure_flag(secure: bool | None = None) -> str:
    return "--secure" if (settings.secure if secure is None else secure) else "--insecure"


def _container(container: str | None) -> str:
    return container or settings.cockroach_container_name


def _changed_response(operation: str, approved: bool, result: dict[str, Any]) -> dict[str, Any]:
    return {"operation": operation, "approved": approved, **result}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_cluster_overview() -> str:
    """Return a compact CockroachDB cluster overview from SQL metadata."""
    try:
        return _json(cockroach_provider.cluster_overview())
    except Exception as exc:
        logger.exception("get_cluster_overview failed")
        return _json({"error": str(exc)})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_node_health() -> str:
    """Return node liveness and range health indicators."""
    try:
        return _json(cockroach_provider.node_health())
    except Exception as exc:
        logger.exception("get_node_health failed")
        return _json({"error": str(exc)})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def list_jobs(status: str | None = None, limit: int = 25) -> str:
    """List recent CockroachDB jobs, optionally filtered by status."""
    try:
        bounded_limit = max(1, min(limit, 100))
        return _json(cockroach_provider.jobs(status=status, limit=bounded_limit))
    except Exception as exc:
        logger.exception("list_jobs failed")
        return _json({"error": str(exc)})


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_kubernetes_status() -> str:
    """Return Kubernetes status for CockroachDB pods, StatefulSets, services, and recent events."""
    try:
        return _json(kubernetes_provider.status())
    except Exception as exc:
        logger.exception("get_kubernetes_status failed")
        return _json({"error": str(exc)})


# Spec: Direct1-sql-connect.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def check_sql_connection() -> str:
    """Run a SQL connection check using SELECT 1."""
    try:
        return _json(cockroach_provider.sql_connect())
    except Exception as exc:
        logger.exception("check_sql_connection failed")
        return _json({"error": str(exc)})


# Spec: Direct2-cluster-setting.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_cluster_setting(setting_name: str) -> str:
    """Read one CockroachDB cluster setting."""
    try:
        return _json(cockroach_provider.get_cluster_setting(setting_name))
    except Exception as exc:
        logger.exception("get_cluster_setting failed")
        return _json({"error": str(exc), "setting_name": setting_name})


# Spec: Direct2-cluster-setting.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def set_cluster_setting(setting_name: str, value: str, approved: bool = False) -> str:
    """Set one CockroachDB cluster setting."""
    try:
        blocked = _require_approval("set_cluster_setting", approved)
        if blocked:
            return _json(blocked)
        return _json(cockroach_provider.set_cluster_setting(setting_name, value))
    except Exception as exc:
        logger.exception("set_cluster_setting failed")
        return _json({"error": str(exc), "setting_name": setting_name, "changed": False})


# Spec: Direct3-zone-config.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def read_zone_config(
    target_type: str | None = None,
    target_name: str | None = None,
    max_rows: int = 100,
) -> str:
    """Read crdb_internal.zones metadata."""
    try:
        return _json(cockroach_provider.read_zone_config(target_type, target_name, max_rows=max_rows))
    except Exception as exc:
        logger.exception("read_zone_config failed")
        return _json({"error": str(exc), "rows": []})


# Spec: Direct4-metrics-health.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def probe_metrics_health(
    pod_names: list[str] | None = None,
    statefulset_name: str | None = None,
    desired_replicas: int | None = None,
    http_port: int | None = None,
    secure: bool | None = None,
    delay_seconds: float = 0,
) -> str:
    """Read /_status/vars metrics from CockroachDB pods."""
    try:
        port = http_port or settings.http_port
        use_secure = settings.secure if secure is None else secure
        first = kubernetes_provider.metrics_health(
            pod_names=pod_names,
            statefulset_name=statefulset_name or settings.statefulset_name,
            desired_replicas=desired_replicas,
            http_port=port,
            secure=use_secure,
        )
        if delay_seconds > 0:
            time.sleep(min(delay_seconds, 300))
            second = kubernetes_provider.metrics_health(
                pod_names=pod_names,
                statefulset_name=statefulset_name or settings.statefulset_name,
                desired_replicas=desired_replicas,
                http_port=port,
                secure=use_secure,
            )
            return _json({"operation": "probe_metrics_health", "passes": [first, second]})
        return _json({"operation": "probe_metrics_health", "passes": [first]})
    except Exception as exc:
        logger.exception("probe_metrics_health failed")
        return _json({"error": str(exc)})


# Spec: Indirect1-exec-init.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def run_cockroach_init(
    pod_name: str | None = None,
    container: str | None = None,
    secure: bool | None = None,
    grpc_port: int | None = None,
    approved: bool = False,
) -> str:
    """Run cockroach init in a pod."""
    try:
        blocked = _require_approval("run_cockroach_init", approved)
        if blocked:
            return _json(blocked)
        pod = pod_name or f"{settings.statefulset_name}-0"
        port = grpc_port or settings.grpc_port
        args = ["init", f"--host=localhost:{port}", _secure_flag(secure)]
        return _json(_changed_response("run_cockroach_init", approved, kubernetes_provider.exec_cockroach(pod, _container(container), args)))
    except Exception as exc:
        logger.exception("run_cockroach_init failed")
        return _json({"error": str(exc), "changed": False})


# Spec: Indirect2-exec-node-id.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def discover_node_id(
    pod_name: str | None = None,
    target_host: str | None = None,
    container: str | None = None,
    secure: bool | None = None,
) -> str:
    """Run cockroach node status --format=csv for node-ID discovery."""
    try:
        pod = pod_name or f"{settings.statefulset_name}-0"
        args = ["node", "status", "--format=csv", _secure_flag(secure)]
        result = kubernetes_provider.exec_cockroach(pod, _container(container), args)
        result.update({"operation": "discover_node_id", "target_host": target_host})
        return _json(result)
    except Exception as exc:
        logger.exception("discover_node_id failed")
        return _json({"error": str(exc)})


# Spec: Indirect3-exec-start-drain.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def start_node_decommission(
    node_id: int,
    pod_name: str | None = None,
    container: str | None = None,
    secure: bool | None = None,
    approved: bool = False,
) -> str:
    """Run cockroach node decommission --wait=none."""
    try:
        blocked = _require_approval("start_node_decommission", approved)
        if blocked:
            return _json(blocked)
        pod = pod_name or f"{settings.statefulset_name}-0"
        args = ["node", "decommission", str(node_id), "--wait=none", _secure_flag(secure)]
        return _json(_changed_response("start_node_decommission", approved, kubernetes_provider.exec_cockroach(pod, _container(container), args)))
    except Exception as exc:
        logger.exception("start_node_decommission failed")
        return _json({"error": str(exc), "changed": False})


# Spec: Indirect4-exec-drain-status.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_decommission_status(
    node_id: int,
    pod_name: str | None = None,
    container: str | None = None,
    secure: bool | None = None,
) -> str:
    """Run cockroach node status <node-id> --decommission --format=csv."""
    try:
        pod = pod_name or f"{settings.statefulset_name}-0"
        args = ["node", "status", str(node_id), "--decommission", "--format=csv", _secure_flag(secure)]
        result = kubernetes_provider.exec_cockroach(pod, _container(container), args)
        result.update({"operation": "get_decommission_status", "node_id": node_id})
        return _json(result)
    except Exception as exc:
        logger.exception("get_decommission_status failed")
        return _json({"error": str(exc)})


# Spec: Indirect5-exec-final-decommission.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def finalize_node_decommission(
    node_id: int,
    pod_name: str | None = None,
    container: str | None = None,
    secure: bool | None = None,
    approved: bool = False,
) -> str:
    """Run final cockroach node decommission."""
    try:
        blocked = _require_approval("finalize_node_decommission", approved)
        if blocked:
            return _json(blocked)
        pod = pod_name or f"{settings.statefulset_name}-0"
        args = ["node", "decommission", str(node_id), _secure_flag(secure)]
        return _json(_changed_response("finalize_node_decommission", approved, kubernetes_provider.exec_cockroach(pod, _container(container), args)))
    except Exception as exc:
        logger.exception("finalize_node_decommission failed")
        return _json({"error": str(exc), "changed": False})


# Spec: Indirect6-start-config.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_start_config(
    pod_name: str | None = None,
    statefulset_name: str | None = None,
) -> str:
    """Read Kubernetes start configuration evidence."""
    try:
        return _json(kubernetes_provider.start_config(pod_name=pod_name, statefulset_name=statefulset_name or settings.statefulset_name))
    except Exception as exc:
        logger.exception("get_start_config failed")
        return _json({"error": str(exc)})


# Spec: Indirect7-stable-status.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_membership_evidence(
    statefulset_name: str | None = None,
    desired_replicas: int | None = None,
) -> str:
    """Read Kubernetes membership evidence for desired pod ordinals."""
    try:
        sts_name = statefulset_name or settings.statefulset_name
        replicas = desired_replicas if desired_replicas is not None else 1
        return _json(kubernetes_provider.membership_evidence(sts_name, replicas))
    except Exception as exc:
        logger.exception("get_membership_evidence failed")
        return _json({"error": str(exc)})


# Spec: Indirect8-statefulset-shrink.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def shrink_statefulset(
    replicas: int,
    statefulset_name: str | None = None,
    approved: bool = False,
) -> str:
    """Patch a StatefulSet replica count."""
    try:
        blocked = _require_approval("shrink_statefulset", approved)
        if blocked:
            return _json(blocked)
        if replicas < 0:
            return _json({"error": "replicas must be non-negative", "changed": False})
        sts_name = statefulset_name or settings.statefulset_name
        return _json(_changed_response("shrink_statefulset", approved, kubernetes_provider.scale_statefulset(sts_name, replicas)))
    except Exception as exc:
        logger.exception("shrink_statefulset failed")
        return _json({"error": str(exc), "changed": False})


# Spec: Indirect9-binary-rollout.
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def get_rollout_evidence(
    statefulset_name: str | None = None,
    partition: int | None = None,
    desired_image: str | None = None,
) -> str:
    """Read StatefulSet partition and pod rollout evidence."""
    try:
        return _json(kubernetes_provider.rollout_evidence(statefulset_name or settings.statefulset_name, partition, desired_image))
    except Exception as exc:
        logger.exception("get_rollout_evidence failed")
        return _json({"error": str(exc)})


# Spec: Indirect10-restart-cleanup.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def cleanup_restart_annotation(
    annotation_key: str,
    resource_name: str | None = None,
    expected_value: str | None = None,
    approved: bool = False,
) -> str:
    """Remove a restart annotation from a StatefulSet."""
    try:
        blocked = _require_approval("cleanup_restart_annotation", approved)
        if blocked:
            return _json(blocked)
        name = resource_name or settings.statefulset_name
        return _json(_changed_response("cleanup_restart_annotation", approved, kubernetes_provider.cleanup_restart_annotation(name, annotation_key, expected_value)))
    except Exception as exc:
        logger.exception("cleanup_restart_annotation failed")
        return _json({"error": str(exc), "changed": False})


# Spec: Indirect11-ingress-host-sync.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def sync_ingress_host(
    name: str,
    host: str = "",
    service_name: str = "cockroachdb",
    service_port: int = 8080,
    tls_secret_name: str | None = None,
    path: str = "/",
    enabled: bool = True,
    approved: bool = False,
) -> str:
    """Create/update or delete an Ingress for a CockroachDB host."""
    try:
        blocked = _require_approval("sync_ingress_host", approved)
        if blocked:
            return _json(blocked)
        if not enabled:
            return _json(_changed_response("sync_ingress_host", approved, kubernetes_provider.delete_ingress(name)))
        if not host:
            return _json({"error": "host is required when enabled=true", "changed": False})
        return _json(_changed_response("sync_ingress_host", approved, kubernetes_provider.sync_ingress_host(name, host, service_name, service_port, tls_secret_name, path)))
    except Exception as exc:
        logger.exception("sync_ingress_host failed")
        return _json({"error": str(exc), "changed": False})


# Spec: Indirect12-version-check-job.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def run_version_check_job(
    image: str,
    expected_version: str | None = None,
    job_name: str | None = None,
    timeout_seconds: int = 120,
    delete_after: bool = True,
    approved: bool = False,
) -> str:
    """Create a Kubernetes Job that runs cockroach version."""
    try:
        blocked = _require_approval("run_version_check_job", approved)
        if blocked:
            return _json(blocked)
        name = job_name or f"cockroachdb-vcheck-{uuid4().hex[:8]}"
        return _json(_changed_response("run_version_check_job", approved, kubernetes_provider.version_check_job(name, image, expected_version, timeout_seconds, delete_after)))
    except Exception as exc:
        logger.exception("run_version_check_job failed")
        return _json({"error": str(exc), "changed": False})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def run_sql(query: str, max_rows: int = 100, approved: bool = False) -> str:
    """Run SQL against CockroachDB.

    SELECT/SHOW/EXPLAIN/WITH statements are allowed as diagnostics. Other
    statements require approval and are blocked when MCP_READ_ONLY is true.
    """
    try:
        bounded_max_rows = max(1, min(max_rows, 1000))
        if _is_read_only_sql(query):
            return _json(cockroach_provider.query(query, max_rows=bounded_max_rows))

        blocked = _require_approval("run_sql", approved)
        if blocked:
            return _json(blocked)
        return _json(cockroach_provider.execute(query))
    except Exception as exc:
        logger.exception("run_sql failed")
        return _json({"error": str(exc), "changed": False})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def trigger_backup(destination: str, approved: bool = False) -> str:
    """Trigger a CockroachDB backup into the provided destination URI."""
    try:
        blocked = _require_approval("trigger_backup", approved)
        if blocked:
            return _json(blocked)
        result = cockroach_provider.execute("BACKUP INTO %s", (destination,))
        return _json({"operation": "trigger_backup", "approved": approved, **result})
    except Exception as exc:
        logger.exception("trigger_backup failed")
        return _json({"error": str(exc), "changed": False})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def scale_statefulset(name: str, replicas: int, approved: bool = False) -> str:
    """Scale a CockroachDB StatefulSet to the requested replica count."""
    try:
        if replicas < 1:
            return _json({"error": "replicas must be at least 1", "changed": False})
        blocked = _require_approval("scale_statefulset", approved)
        if blocked:
            return _json(blocked)
        return _json(kubernetes_provider.scale_statefulset(name=name, replicas=replicas))
    except Exception as exc:
        logger.exception("scale_statefulset failed")
        return _json({"error": str(exc), "changed": False})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def restart_pod(pod_name: str, approved: bool = False) -> str:
    """Restart a CockroachDB pod by deleting it and allowing Kubernetes to recreate it."""
    try:
        blocked = _require_approval("restart_pod", approved)
        if blocked:
            return _json(blocked)
        return _json(kubernetes_provider.restart_pod(pod_name=pod_name))
    except Exception as exc:
        logger.exception("restart_pod failed")
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
