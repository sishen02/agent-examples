"""CockroachDB SQL provider.

The psycopg dependency is imported lazily so tests and read-only package
inspection do not require a live database client installation.
"""

from typing import Any


class NullCockroachProvider:
    """Provider used when COCKROACH_DSN is not configured."""

    def __init__(self, reason: str = "COCKROACH_DSN is not configured"):
        self.reason = reason

    def query(self, sql: str, params: tuple[Any, ...] | None = None, max_rows: int = 100) -> dict[str, Any]:
        return {"error": self.reason, "sql": sql, "columns": [], "rows": [], "row_count": 0, "truncated": False}

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any]:
        return {"error": self.reason, "sql": sql, "changed": False}

    def cluster_overview(self) -> dict[str, Any]:
        return {"source": "unconfigured", "nodes": [], "databases": [], "error": self.reason}

    def node_health(self) -> dict[str, Any]:
        return {"error": self.reason, "nodes": []}

    def jobs(self, status: str | None = None, limit: int = 25) -> dict[str, Any]:
        return {"error": self.reason, "jobs": []}


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

    def cluster_overview(self) -> dict[str, Any]:
        version = self.query("SELECT version()", max_rows=1)
        databases = self.query("SHOW DATABASES", max_rows=200)
        database_names = [row.get("database_name") or row.get("Database") for row in databases.get("rows", [])]
        return {
            "source": "sql_safe",
            "version": version.get("rows", [{}])[0].get("version") if version.get("rows") else None,
            "nodes": [],
            "databases": [name for name in database_names if name],
            "notes": [
                "Node metadata is not queried by default because it depends on CockroachDB internal interfaces.",
            ],
        }

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

    def jobs(self, status: str | None = None, limit: int = 25) -> dict[str, Any]:
        params: tuple[Any, ...]
        if status:
            sql = """
                SELECT job_id, job_type, description, status, created, started, finished, fraction_completed
                FROM [SHOW JOBS]
                WHERE status = %s
                ORDER BY created DESC
                LIMIT %s
            """
            params = (status, limit)
        else:
            sql = """
                SELECT job_id, job_type, description, status, created, started, finished, fraction_completed
                FROM [SHOW JOBS]
                ORDER BY created DESC
                LIMIT %s
            """
            params = (limit,)
        result = self.query(sql, params=params, max_rows=limit)
        return {"jobs": result.get("rows", [])}
