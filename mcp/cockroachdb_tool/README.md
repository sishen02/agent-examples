# CockroachDB MCP Tool

FastMCP server for CockroachDB operations. It is designed to be used by an agent, with deterministic tool boundaries and approval-gated mutations.

## Tools

- `get_cluster_overview()` - CockroachDB version, nodes, and databases.
- `get_node_health()` - node liveness and range health indicators.
- `list_jobs(status, limit)` - recent CockroachDB jobs.
- `get_kubernetes_status()` - pods, StatefulSets, services, and recent events.
- `run_sql(query, max_rows, approved)` - diagnostic SQL by default; mutating SQL requires approval.
- `trigger_backup(destination, approved)` - starts a backup.
- `scale_statefulset(name, replicas, approved)` - scales a CockroachDB StatefulSet.
- `restart_pod(pod_name, approved)` - restarts a pod by deleting it.

## Safety Defaults

The server fails closed:

- `MCP_READ_ONLY=true` blocks every mutating tool.
- `REQUIRE_APPROVAL=true` requires `approved=true` for mutating tools.
- Read-only SQL is limited to statements beginning with `SELECT`, `SHOW`, `EXPLAIN`, or `WITH`.

## Configuration

```bash
export COCKROACH_DSN="postgresql://root@cockroach-public:26257/defaultdb?sslmode=require"
export K8S_NAMESPACE="default"
export COCKROACH_LABEL_SELECTOR="app.kubernetes.io/name=cockroachdb"
export MCP_READ_ONLY=true
export REQUIRE_APPROVAL=true
```

Optional:

```bash
export ENABLE_KUBERNETES=true
export HOST=0.0.0.0
export PORT=8000
export MCP_TRANSPORT=http
```

## Run

```bash
uv sync
uv run cockroachdb_tool.py
```

