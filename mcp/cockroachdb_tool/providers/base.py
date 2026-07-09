"""Provider interfaces for CockroachDB and Kubernetes operations."""

from typing import Any, Protocol


class CockroachProvider(Protocol):
    def query(self, sql: str, params: tuple[Any, ...] | None = None, max_rows: int = 100) -> dict[str, Any]:
        """Run SQL and return columns, rows, and row count."""

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any]:
        """Run a mutating SQL/admin statement."""

    def node_health(self) -> dict[str, Any]:
        """Return node health/status details."""


class KubernetesProvider(Protocol):
    def status(self) -> dict[str, Any]:
        """Return relevant Kubernetes resource status."""

    def scale_statefulset(self, name: str, replicas: int) -> dict[str, Any]:
        """Scale a StatefulSet."""

    def restart_pod(self, pod_name: str) -> dict[str, Any]:
        """Delete a pod so Kubernetes recreates it."""

    def delete_pod(self, pod_name: str) -> dict[str, Any]:
        """Delete a pod."""

    def exec_cockroach(self, pod_name: str, container: str, args: list[str]) -> dict[str, Any]:
        """Run a cockroach CLI command in a pod."""

    def metrics_health(
        self,
        pod_names: list[str] | None = None,
        statefulset_name: str | None = None,
        desired_replicas: int | None = None,
        http_port: int = 8080,
        secure: bool = False,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """Read /_status/vars metrics from CockroachDB pods."""
