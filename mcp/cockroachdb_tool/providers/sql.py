"""CockroachDB SQL provider for benchmark operations."""

from typing import Any


class NullCockroachProvider:
    """Provider used when COCKROACH_DSN is not configured."""

    def __init__(self, reason: str = "COCKROACH_DSN is not configured"):
        self.reason = reason

    def query(self, sql: str, params: tuple[Any, ...] | None = None, max_rows: int = 100) -> dict[str, Any]:
        return {"error": self.reason, "sql": sql, "columns": [], "rows": [], "row_count": 0, "truncated": False}

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any]:
        return {"error": self.reason, "sql": sql, "changed": False}

    def node_health(self) -> dict[str, Any]:
        return {"error": self.reason, "nodes": []}


class CockroachSQLProvider:
    """CockroachDB SQL/Admin API provider using psycopg."""

    def __init__(self, dsn: str, connect_timeout: int = 10):
        self.dsn = dsn
        self.connect_timeout = connect_timeout

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, connect_timeout=self.connect_timeout, row_factory=dict_row)

    def query(self, sql: str, params: tuple[Any, ...] | None = None, max_rows: int = 100) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchmany(max_rows + 1) if cur.description else []
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
                columns = [desc.name for desc in cur.description] if cur.description else []
                return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row_count = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            conn.commit()
        return {"changed": True, "row_count": row_count}

    def node_health(self) -> dict[str, Any]:
        sql_probe = self.query("SELECT 1 AS sql_available, now() AS checked_at", max_rows=1)
        return {
            "source": "sql_safe",
            "sql_available": bool(sql_probe.get("rows")),
            "checked_at": sql_probe.get("rows", [{}])[0].get("checked_at") if sql_probe.get("rows") else None,
            "nodes": [],
            "range_summary": [],
            "notes": [
                "Node liveness, range health, and job metadata are not queried by default because they can depend on restricted CockroachDB internal/system interfaces.",
            ],
        }
