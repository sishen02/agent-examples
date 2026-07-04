"""Provider implementations for CockroachDB MCP tools."""

try:
    from cockroachdb_tool.providers.base import CockroachProvider, KubernetesProvider
    from cockroachdb_tool.providers.kubernetes import KubernetesAPIProvider, NullKubernetesProvider
    from cockroachdb_tool.providers.sql import CockroachSQLProvider, NullCockroachProvider
except ImportError:
    from providers.base import CockroachProvider, KubernetesProvider
    from providers.kubernetes import KubernetesAPIProvider, NullKubernetesProvider
    from providers.sql import CockroachSQLProvider, NullCockroachProvider

__all__ = [
    "CockroachProvider",
    "CockroachSQLProvider",
    "KubernetesAPIProvider",
    "KubernetesProvider",
    "NullCockroachProvider",
    "NullKubernetesProvider",
]
