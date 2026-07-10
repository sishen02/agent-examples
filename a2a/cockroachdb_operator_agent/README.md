# CockroachDB Operator Agent

A2A agent for CockroachDB SRE/DBA workflows. It uses the CockroachDB MCP tool server for deterministic inspection and operational actions.

## What It Does

- Diagnoses CockroachDB health using MCP tool state projections.
- Inspects Kubernetes pods, StatefulSets, services, and events.
- Produces evidence-based operational plans.
- Executes backup creation, scaling, node restart, node decommission, and volume expansion through MCP tools.

## Configuration

Template env files are provided:

- `.env.openai` - OpenAI-compatible production-style config with secret refs
- `.env.vllm` - local VLLM config

```bash
export MCP_URL="http://cockroach-db-tool-mcp.team1.svc.cluster.local:9090/mcp"
export MCP_TRANSPORT="streamable_http"
export LLM_MODEL="llama3.2:3b-instruct-fp16"
export LLM_API_BASE="http://172.19.0.1:8000/v1"
export LLM_API_KEY="api-key"
export TRAJECTORY_ENABLED=true
export TRAJECTORY_DIR=/shared/trajectories
```

Conversation history is held in memory per A2A `context_id` and bounded by
`MAX_HISTORY_MESSAGES`. A pod restart clears this history.

## Trajectory Files

Each A2A `context_id` writes one JSON trajectory file when
`TRAJECTORY_ENABLED=true`. Later requests in the same context update the same
file, appending turn details and replacing the top-level `messages` array with
the full linear conversation history: user prompts, assistant tool calls, tool
results, and final assistant responses.

For persistence across pod restarts, deploy this agent as a StatefulSet with
persistent storage enabled. Kagenti mounts that storage at `/shared`, so the
default trajectory directory is `/shared/trajectories`.

Inspect or copy saved trajectories with:

```bash
kubectl exec -n team1 statefulset/cockroach-db-agent -- ls -lah /shared/trajectories
kubectl cp team1/cockroach-db-agent-0:/shared/trajectories ./trajectories
```

## Run

```bash
uv sync
uv run server
```

Example prompts:

- `Why is my CockroachDB cluster unhealthy?`
- `Check node and pod health.`
- `Plan a safe restart for pod cockroachdb-1.`
- `Create a CockroachDB backup.`
