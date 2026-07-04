# CockroachDB Operator Agent

A2A agent for CockroachDB SRE/DBA workflows. It uses the CockroachDB MCP tool server for deterministic inspection and approval-gated operational actions.

## What It Does

- Diagnoses CockroachDB health using SQL/admin metadata.
- Inspects Kubernetes pods, StatefulSets, services, and events.
- Produces evidence-based operational plans.
- Executes risky operations only after explicit user approval and only through MCP tools.

It is not a background reconciliation controller.

## Configuration

Template env files are provided:

- `.env.openai` - OpenAI-compatible production-style config with secret refs
- `.env.vllm` - local VLLM config

```bash
export MCP_URL="http://cockroachdb-tool:8000/mcp"
export MCP_TRANSPORT="streamable_http"
export LLM_MODEL="llama3.2:3b-instruct-fp16"
export LLM_API_BASE="http://172.19.0.1:8000/v1"
export LLM_API_KEY="api-key"
```

## Run

```bash
uv sync
uv run server
```

Example prompts:

- `Why is my CockroachDB cluster unhealthy?`
- `Check node health and recent failed jobs.`
- `Plan a safe restart for pod cockroachdb-1.`
- `I approve restarting pod cockroachdb-1.`
