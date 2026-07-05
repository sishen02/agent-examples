"""Configuration for the CockroachDB operator agent."""

from pydantic_settings import BaseSettings


class Configuration(BaseSettings):
    LLM_MODEL: str = "model"
    LLM_API_BASE: str = "http://172.19.0.1:8000/v1"
    LLM_API_KEY: str = "api-key"
    MCP_URL: str = "http://cockroach-db-tool-mcp.team1.svc.cluster.local:9090/mcp"
    MCP_TRANSPORT: str = "streamable_http"
    MCP_TIMEOUT: int = 600
    MAX_EVENT_DISPLAY_LENGTH: int = 384
    MAX_HISTORY_MESSAGES: int = 20
    AGENT_VERSION: str = "1.0.0"
