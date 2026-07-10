"""Shared schemas for the CockroachDB MCP tool."""

from typing import Any, Literal

from pydantic import BaseModel, Field


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
