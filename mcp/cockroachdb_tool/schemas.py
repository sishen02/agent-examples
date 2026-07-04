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
    source: Literal["sql", "unconfigured"]
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
    approved: bool
    changed: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

