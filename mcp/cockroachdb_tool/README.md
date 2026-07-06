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

## Safety Settings

By default, the example allows mutating tools without extra approval gates:

- `MCP_READ_ONLY=false` allows mutating tools.
- `REQUIRE_APPROVAL=false` does not require `approved=true` for mutating tools.
- Set `MCP_READ_ONLY=true` to block every mutating tool.
- Set `REQUIRE_APPROVAL=true` to require `approved=true` for mutating tools.
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
export MCP_READ_ONLY=false
export REQUIRE_APPROVAL=false
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

Use the MCP endpoint path `/mcp` without a trailing slash.

The default `COCKROACH_DSN` uses Kubernetes cluster DNS and is intended for
in-cluster deployment. When running the tool from a workstation, port-forward
the CockroachDB SQL service and override the DSN, for example:

```bash
kubectl -n cockroachdb port-forward svc/cockroachdb 26257:26257
export COCKROACH_DSN="postgresql://root@127.0.0.1:26257/defaultdb?sslmode=disable"
```

The Kagenti Kind example manifest deploys a single-node insecure CockroachDB
StatefulSet in the `cockroachdb` namespace with service name `cockroachdb`.
It is still a single-node `start-single-node` example; increasing StatefulSet
replicas does not create a valid multi-node CockroachDB cluster.
