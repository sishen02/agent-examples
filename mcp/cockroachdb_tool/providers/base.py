"""Provider interfaces for CockroachDB and Kubernetes operations."""

from typing import Any, Protocol


class CockroachProvider(Protocol):
    def query(self, sql: str, params: tuple[Any, ...] | None = None, max_rows: int = 100) -> dict[str, Any]:
        """Run SQL and return columns, rows, and row count."""

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any]:
        """Run a mutating SQL/admin statement."""

    def cluster_overview(self) -> dict[str, Any]:
        """Return a compact cluster overview."""

    def node_health(self) -> dict[str, Any]:
        """Return node health/status details."""

    def jobs(self, status: str | None = None, limit: int = 25) -> dict[str, Any]:
        """Return recent jobs."""


class KubernetesProvider(Protocol):
    def status(self) -> dict[str, Any]:
        """Return relevant Kubernetes resource status."""

    def scale_statefulset(self, name: str, replicas: int) -> dict[str, Any]:
        """Scale a StatefulSet."""

    def restart_pod(self, pod_name: str) -> dict[str, Any]:
        """Delete a pod so Kubernetes recreates it."""

