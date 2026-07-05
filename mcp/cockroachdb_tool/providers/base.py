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

    def sql_connect(self) -> dict[str, Any]:
        """Run a SQL connection probe."""

    def get_cluster_setting(self, setting_name: str) -> dict[str, Any]:
        """Read a cluster setting."""

    def set_cluster_setting(self, setting_name: str, value: str) -> dict[str, Any]:
        """Write a cluster setting."""

    def read_zone_config(
        self,
        target_type: str | None = None,
        target_name: str | None = None,
        max_rows: int = 100,
    ) -> dict[str, Any]:
        """Read zone config metadata."""


class KubernetesProvider(Protocol):
    def status(self) -> dict[str, Any]:
        """Return relevant Kubernetes resource status."""

    def scale_statefulset(self, name: str, replicas: int) -> dict[str, Any]:
        """Scale a StatefulSet."""

    def restart_pod(self, pod_name: str) -> dict[str, Any]:
        """Delete a pod so Kubernetes recreates it."""

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
