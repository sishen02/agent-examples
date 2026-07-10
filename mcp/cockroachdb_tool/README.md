# CockroachDB MCP Tool

FastMCP server for CockroachDB operations. It is designed to be used by an agent, with deterministic tool boundaries.

## Tools

- `get_cluster_status(namespace, cluster)` - typed cluster health and readiness projection.
- `list_database_nodes(namespace, cluster)` - typed node/pod state.
- `get_node_status(namespace, cluster, node_id)` - typed state for one CockroachDB node.
- `get_storage_status(namespace, cluster)` - PVC/storage state for volume operations.
- `get_backup_status(namespace, cluster)` - latest known backup state.
- `drain_cockroach_node(namespace, cluster, node_id)` - start drain/decommission protocol without deleting pods or PVCs.
- `wait_for_node_ready(namespace, cluster, node_id, timeout_seconds)` - wait for one node to become ready.
- `wait_for_cluster_healthy(namespace, cluster, timeout_seconds)` - wait for the cluster health projection to pass.
- `restart_cockroach_node(namespace, cluster, node_id)` - restart exactly one CockroachDB node.
- `delete_cockroach_pod(namespace, cluster, pod_name)` - delete one CockroachDB pod by Kubernetes pod name.
- `scale_cockroach_statefulset(namespace, cluster, target_replicas)` - scale the CockroachDB StatefulSet replica count.
- `decommission_cockroach_node(namespace, cluster, node_id)` - permanently decommission one CockroachDB node without deleting PVCs.
- `expand_data_volume(namespace, cluster, node_id, target_size_gib)` - expand one data PVC upward only.
- `create_backup(namespace, cluster, backup_scope, database)` - request a CockroachDB backup.

Inspection tools return structured state projections. Mutation and wait tools
return concise text describing what was requested or an `Error:` message when
the operation fails.

## Configuration

Use `.env.template` as the reference for tool runtime environment variables.

```bash
export COCKROACH_DSN="postgresql://root@cockroachdb.cockroachdb.svc.cluster.local:26257/defaultdb?sslmode=disable"
export BACKUP_DESTINATION="nodelocal://1/cockroachdb-tool"
export K8S_NAMESPACE="cockroachdb"
export COCKROACH_LABEL_SELECTOR="app.kubernetes.io/name=cockroachdb"
export STATEFULSET_NAME="cockroachdb"
export COCKROACH_CONTAINER_NAME="cockroachdb"
export GRPC_PORT=26357
export HTTP_PORT=8080
export SECURE=false
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

The Kagenti Kind example manifest deploys a three-node insecure CockroachDB
StatefulSet in the `cockroachdb` namespace with service name `cockroachdb`.
The StatefulSet uses stable pod DNS names for node discovery and the Kind
startup script initializes the cluster after rollout.
