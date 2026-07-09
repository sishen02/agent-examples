"""Shared schemas for the CockroachDB MCP tool."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolError(BaseModel):
    error: str
    details: str | None = None


class SQLResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


class ClusterOverview(BaseModel):
    source: Literal["sql", "sql_safe", "unconfigured"]
    cluster_id: str | None = None
    organization: str | None = None
    version: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)


class KubernetesStatus(BaseModel):
    namespace: str
    label_selector: str
    pods: list[dict[str, Any]] = Field(default_factory=list)
    statefulsets: list[dict[str, Any]] = Field(default_factory=list)
    services: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)


class OperationReceipt(BaseModel):
    operation: str
    status: Literal["success", "failed"] = "success"
    changed: bool
    message: str
    state_before: dict[str, Any] = Field(default_factory=dict)
    state_after: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class ClusterStatus(BaseModel):
    namespace: str
    cluster: str
    cluster_phase: Literal["Ready", "Degraded", "Reconciling", "Unknown"] = "Unknown"
    desired_replicas: int = 0
    ready_replicas: int = 0
    live_cockroach_nodes: int = 0
    unavailable_ranges: int = 0
    under_replicated_ranges: int = 0
    sql_ready: bool = False
    operator_ready: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class NodeInfo(BaseModel):
    node_id: int
    pod_name: str
    kubernetes_node: str | None = None
    pod_ready: bool = False
    cockroach_live: bool = False
    draining: bool = False
    decommissioning: bool = False
    disk_used_percent: float | None = None
    version: str | None = None


class NodeStatus(NodeInfo):
    namespace: str
    cluster: str
    exists: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)


class StorageStatus(BaseModel):
    namespace: str
    cluster: str
    volumes: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class BackupStatus(BaseModel):
    namespace: str
    cluster: str
    latest_successful_backup_time: str | None = None
    backup_location: str | None = None
    recent_enough: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class WaitResult(OperationReceipt):
    timed_out: bool = False


class DrainResult(OperationReceipt):
    node_id: int


class RestartResult(OperationReceipt):
    node_id: int


class PodDeleteResult(OperationReceipt):
    pod_name: str


class ScaleResult(OperationReceipt):
    target_replicas: int


class DecommissionResult(OperationReceipt):
    node_id: int


class VolumeExpansionResult(OperationReceipt):
    node_id: int
    target_size_gib: int


class BackupResult(OperationReceipt):
    backup_id: str | None = None
