# CockroachDB MCP Tool

FastMCP server for CockroachDB operations. It is designed to be used by an agent, with deterministic tool boundaries and approval-gated mutations.

## Tools

General diagnostics:

- `get_cluster_overview()` - CockroachDB version and databases using safe SQL metadata.
- `get_node_health()` - SQL reachability using safe SQL metadata.
- `list_jobs(status, limit)` - recent CockroachDB jobs.
- `get_kubernetes_status()` - pods, StatefulSets, services, and recent events.
- `run_sql(query, max_rows, approved)` - diagnostic SQL by default; mutating SQL requires approval.
- `trigger_backup(destination, approved)` - starts a backup.
- `scale_statefulset(name, replicas, approved)` - scales a CockroachDB StatefulSet.
- `restart_pod(pod_name, approved)` - restarts a pod by deleting it.

Operation tools from `spec.md`:

- `check_sql_connection()` - SQL `SELECT 1` connection probe.
- `get_cluster_setting(setting_name)` and `set_cluster_setting(setting_name, value, approved)` - `SHOW/SET CLUSTER SETTING`.
- `read_zone_config(target_type, target_name, max_rows)` - reads `crdb_internal.zones`.
- `probe_metrics_health(...)` - reads `/_status/vars` from CockroachDB pods.
- `run_cockroach_init(...)` - runs `cockroach init`.
- `discover_node_id(...)` - runs `cockroach node status --format=csv`.
- `start_node_decommission(...)` - runs `cockroach node decommission --wait=none`.
- `get_decommission_status(...)` - runs `cockroach node status <node-id> --decommission --format=csv`.
- `finalize_node_decommission(...)` - runs final `cockroach node decommission`.
- `get_start_config(...)` - returns pod/StatefulSet start configuration evidence.
- `get_membership_evidence(...)` - returns desired pod ordinal evidence.
- `shrink_statefulset(...)` - patches StatefulSet replicas.
- `get_rollout_evidence(...)` - returns StatefulSet/pod rollout evidence.
- `cleanup_restart_annotation(...)` - removes a restart annotation.
- `sync_ingress_host(...)` - creates, updates, or deletes an Ingress.
- `run_version_check_job(...)` - runs a CockroachDB image version-check Job.

These tools execute the named operation and return evidence. They do not enforce
the preconditions and postconditions described in `spec.md`.

## Safety Defaults

The server fails closed:

- `MCP_READ_ONLY=true` blocks every mutating tool.
- `REQUIRE_APPROVAL=true` requires `approved=true` for mutating tools.
- Read-only SQL is limited to statements beginning with `SELECT`, `SHOW`, `EXPLAIN`, or `WITH`.
- Default overview and health tools avoid `crdb_internal` metadata so they work in environments that restrict CockroachDB internal interfaces.

## Configuration

Use `.env.template` as the reference for tool runtime environment variables.

```bash
export COCKROACH_DSN="postgresql://root@cockroachdb.cockroachdb.svc.cluster.local:26257/defaultdb?sslmode=disable"
export K8S_NAMESPACE="cockroachdb"
export COCKROACH_LABEL_SELECTOR="app.kubernetes.io/name=cockroachdb"
export STATEFULSET_NAME="cockroachdb"
export COCKROACH_CONTAINER_NAME="cockroachdb"
export GRPC_PORT=26257
export HTTP_PORT=8080
export SECURE=false
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

## Kubernetes RBAC

When the tool is deployed by Kagenti in `team1` as `cockroach-db-tool`, it needs
read permission in the `cockroachdb` namespace to inspect pods, StatefulSets,
services, and events:

```bash
kubectl apply -f kagenti/kagenti/examples/databases/cockroachdb-tool-rbac.yaml
```

The Kind installer also applies this manifest when run with `--with-cockroachdb`.

## Run

```bash
uv sync
uv run cockroachdb_tool.py
```

The Kagenti Kind example manifest deploys a single-node insecure CockroachDB
Deployment in the `cockroachdb` namespace with service name `cockroachdb`.
Because that sample is a Deployment, `scale_statefulset()` is intended for
production-style CockroachDB StatefulSet deployments, not that local sample.
