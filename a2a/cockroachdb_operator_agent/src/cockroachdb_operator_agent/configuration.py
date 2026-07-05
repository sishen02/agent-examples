"""Configuration for the CockroachDB operator agent."""

from pydantic_settings import BaseSettings


class Configuration(BaseSettings):
    LLM_MODEL: str = "llama3.2:3b-instruct-fp16"
    LLM_API_BASE: str = "http://host.docker.internal:11434/v1"
    LLM_API_KEY: str = "dummy"
    MCP_URL: str = "http://cockroachdb-tool-mcp.cockroachdb.svc.cluster.local:8000/mcp"
    MCP_TRANSPORT: str = "streamable_http"
    MCP_TIMEOUT: int = 600
    MAX_EVENT_DISPLAY_LENGTH: int = 384
    MAX_HISTORY_MESSAGES: int = 20
    AGENT_VERSION: str = "1.0.0"
