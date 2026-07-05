"""CockroachDB MCP server for agentic database operations."""

import json
import logging
import os
import re
import sys
from typing import Any

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
    enable_kubernetes: bool = True
    mcp_read_only: bool = True
    require_approval: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
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
