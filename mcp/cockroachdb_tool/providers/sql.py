"""CockroachDB SQL provider.

The psycopg dependency is imported lazily so tests and read-only package
inspection do not require a live database client installation.
"""

import re
from typing import Any


_SETTING_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")


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

    def sql_connect(self) -> dict[str, Any]:
        return {"error": self.reason, "sql_available": False}

    def get_cluster_setting(self, setting_name: str) -> dict[str, Any]:
        return {"error": self.reason, "setting_name": setting_name}

    def set_cluster_setting(self, setting_name: str, value: str) -> dict[str, Any]:
        return {"error": self.reason, "setting_name": setting_name, "value": value, "changed": False}

    def read_zone_config(
        self,
        target_type: str | None = None,
        target_name: str | None = None,
        max_rows: int = 100,
    ) -> dict[str, Any]:
        return {"error": self.reason, "rows": []}


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

    def sql_connect(self) -> dict[str, Any]:
        result = self.query("SELECT 1 AS ok", max_rows=1)
        ok = bool(result.get("rows")) and result["rows"][0].get("ok") == 1
        return {"operation": "check_sql_connection", "sql_available": ok, **result}

    def get_cluster_setting(self, setting_name: str) -> dict[str, Any]:
        _validate_setting_name(setting_name)
        result = self.query(f"SHOW CLUSTER SETTING {setting_name}", max_rows=2)
        return {"operation": "get_cluster_setting", "setting_name": setting_name, **result}

    def set_cluster_setting(self, setting_name: str, value: str) -> dict[str, Any]:
        _validate_setting_name(setting_name)
        result = self.execute(f"SET CLUSTER SETTING {setting_name} = %s", (value,))
        return {
            "operation": "set_cluster_setting",
            "setting_name": setting_name,
            "value": value,
            **result,
        }

    def read_zone_config(
        self,
        target_type: str | None = None,
        target_name: str | None = None,
        max_rows: int = 100,
    ) -> dict[str, Any]:
        bounded_max_rows = max(1, min(max_rows, 1000))
        clauses: list[str] = []
        params: list[Any] = []
        if target_type:
            clauses.append("target_type = %s")
            params.append(target_type)
        if target_name:
            clauses.append("target_name = %s")
            params.append(target_name)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT target_type, target_name, config_sql, full_config_yaml
            FROM crdb_internal.zones
            {where}
            ORDER BY target_type, target_name
        """
        result = self.query(sql, params=tuple(params) or None, max_rows=bounded_max_rows)
        return {
            "operation": "read_zone_config",
            "target_type": target_type,
            "target_name": target_name,
            **result,
        }


def _validate_setting_name(setting_name: str) -> None:
    if not _SETTING_NAME.fullmatch(setting_name):
        raise ValueError(f"invalid cluster setting name: {setting_name!r}")
