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
        nodes = self.query(
            """
            SELECT node_id, address, sql_address, build, started_at, updated_at, locality, is_available, is_live
            FROM crdb_internal.gossip_nodes
            ORDER BY node_id
            """,
            max_rows=100,
        )
        databases = self.query("SHOW DATABASES", max_rows=200)
        database_names = [row.get("database_name") or row.get("Database") for row in databases.get("rows", [])]
        return {
            "source": "sql",
            "version": version.get("rows", [{}])[0].get("version") if version.get("rows") else None,
            "nodes": nodes.get("rows", []),
            "databases": [name for name in database_names if name],
        }

    def node_health(self) -> dict[str, Any]:
        nodes = self.query(
            """
            SELECT node_id, address, is_available, is_live, updated_at, locality
            FROM crdb_internal.gossip_nodes
            ORDER BY node_id
            """,
            max_rows=100,
        )
        ranges = self.query(
            """
            SELECT count(*) AS unavailable_ranges
            FROM crdb_internal.ranges
            WHERE array_length(replicas, 1) = 0
            """,
            max_rows=1,
        )
        return {"nodes": nodes.get("rows", []), "range_summary": ranges.get("rows", [])}

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

