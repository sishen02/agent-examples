# CockroachDB MCP Tool

FastMCP server for CockroachDB operations. It is designed to be used by an agent, with deterministic tool boundaries and approval-gated mutations.

## Tools

- `get_cluster_status(namespace, cluster)` - typed cluster health and readiness projection.
- `list_database_nodes(namespace, cluster)` - typed node/pod state.
- `get_node_status(namespace, cluster, node_id)` - typed state for one CockroachDB node.
- `get_storage_status(namespace, cluster)` - PVC/storage state for volume operations.
- `get_backup_status(namespace, cluster)` - latest known backup state.
- `drain_cockroach_node(namespace, cluster, node_id, approved)` - start drain/decommission protocol without deleting pods or PVCs.
- `wait_for_node_ready(namespace, cluster, node_id, timeout_seconds)` - wait for one node to become ready.
- `wait_for_cluster_healthy(namespace, cluster, timeout_seconds)` - wait for the cluster health projection to pass.
- `restart_cockroach_node(namespace, cluster, node_id, approved)` - restart exactly one CockroachDB node.
- `scale_cockroach_cluster(namespace, cluster, target_replicas, approved)` - scale the cluster through a guarded semantic operation.
- `decommission_cockroach_node(namespace, cluster, node_id, approved)` - permanently decommission one CockroachDB node without deleting PVCs.
- `expand_data_volume(namespace, cluster, node_id, target_size_gib, approved)` - expand one data PVC upward only.
- `create_backup(namespace, cluster, backup_scope, database, approved)` - create a typed backup operation receipt.

These tools return structured receipts with operation name, status, change
flag, message, evidence, and pre/post state where relevant. They add basic
server-side guards, but they are still designed to be paired with an external
AGENT-C/runtime-verification layer for full temporal policy checking.

## Safety Settings

By default, the example allows mutating tools without extra approval gates:

- `MCP_READ_ONLY=false` allows mutating tools.
- `REQUIRE_APPROVAL=false` does not require `approved=true` for mutating tools.
- Set `MCP_READ_ONLY=true` to block every mutating tool.
- Set `REQUIRE_APPROVAL=true` to require `approved=true` for mutating tools.

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
