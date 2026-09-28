"""SQL analytics MCP server — read-only access to the market warehouse.

The connection is opened **read-only** (``file:...?mode=ro``) *and* every statement
passes :mod:`maia.sql_guard`. Two independent layers, because either alone is
insufficient: the URI mode blocks writes at the engine, the guard blocks a
malicious statement before it reaches the engine, and the allowlist is derived
from the live ``sqlite_master`` so an unknown table cannot be queried by name.

Tools: ``list_tables``, ``describe_table``, ``run_sql`` (guarded SELECT with
parameter binding) and ``game_kpi_summary`` (a fixed parameterised query — what an
agent should prefer, because it cannot be talked into an unexpected statement).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ...config import settings
from ...sql_guard import SQLGuardError, guard
from ..protocol import ToolResult
from ..server import MCPServer
from ._common import now_iso

__all__ = ["build_sql_analytics_server", "open_readonly"]

_INTERNAL_PREFIXES = ("sqlite_", "maia_")
_MAX_ROWS = 500


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open SQLite strictly read-only.

    ``mode=ro`` makes an accidental write fail at the engine level even if a
    future change bypasses the guard — defence in depth, not decoration.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"market warehouse {path} does not exist; run ingestion first "
            "(POST /api/v1/market/ingest)"
        )
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def _user_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(r["name"]) for r in rows if not str(r["name"]).startswith(_INTERNAL_PREFIXES)]


def _missing(result_error: str, message: str) -> ToolResult:
    return ToolResult.text(message, structured={"error": result_error}, is_error=True)


def build_sql_analytics_server(db_path: str | Path | None = None) -> MCPServer:
    """Build the ``sql_analytics`` server bound to the market warehouse."""
    path = Path(db_path or settings.MARKET_DB_PATH)
    server = MCPServer(
        "sql_analytics",
        title="Marketing/Product analytics (read-only SQL)",
        instructions=(
            "Query the marketing warehouse. Only SELECT/WITH is allowed, tables are "
            "allowlisted from the live schema and results are row-capped. Prefer "
            "game_kpi_summary over run_sql for standard questions."
        ),
    )

    def _connect() -> sqlite3.Connection:
        return open_readonly(path)

    @server.tool(
        "list_tables",
        description="List the tables available in the marketing warehouse.",
        read_only=True,
        idempotent=True,
        input_schema={"type": "object", "additionalProperties": False, "properties": {}},
    )
    def list_tables(args: dict[str, Any]) -> ToolResult:
        del args
        try:
            with _connect() as connection:
                tables = _user_tables(connection)
        except FileNotFoundError as exc:
            return _missing("warehouse_missing", str(exc))
        return ToolResult.json(
            {"tables": tables, "count": len(tables), "database": str(path)}
        )

    @server.tool(
        "describe_table",
        description="Show the columns and row count of one warehouse table.",
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["table"],
            "additionalProperties": False,
            "properties": {"table": {"type": "string", "minLength": 1, "maxLength": 64}},
        },
    )
    def describe_table(args: dict[str, Any]) -> ToolResult:
        table = str(args["table"])
        try:
            with _connect() as connection:
                allowed = _user_tables(connection)
                if table not in allowed:
                    return _missing("table_not_allowed",
                                    f"unknown table {table!r}; allowed: {allowed}")
                columns = [
                    {"name": r["name"], "type": r["type"], "notnull": bool(r["notnull"]),
                     "pk": bool(r["pk"])}
                    for r in connection.execute(f"PRAGMA table_info({table})").fetchall()
                ]
                count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except FileNotFoundError as exc:
            return _missing("warehouse_missing", str(exc))
        return ToolResult.json({"table": table, "columns": columns, "row_count": count})

    @server.tool(
        "run_sql",
        description=(
            "Run one read-only SELECT against the warehouse. Multi-statement, DDL, "
            "DML and unknown tables are rejected; results are row-capped."
        ),
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["sql"],
            "additionalProperties": False,
            "properties": {
                "sql": {"type": "string", "minLength": 8, "maxLength": 4000},
                "params": {"type": "array", "maxItems": 20},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": _MAX_ROWS},
            },
        },
    )
    def run_sql(args: dict[str, Any]) -> ToolResult:
        sql = str(args["sql"])
        params = list(args.get("params") or [])
        max_rows = int(args.get("max_rows", _MAX_ROWS))
        try:
            with _connect() as connection:
                plan = guard(
                    sql,
                    allowed_tables=frozenset(_user_tables(connection)),
                    max_rows=max_rows,
                )
                rows = connection.execute(plan.sql, params).fetchall()
                columns = list(rows[0].keys()) if rows else []
        except FileNotFoundError as exc:
            return _missing("warehouse_missing", str(exc))
        except SQLGuardError as exc:
            return _missing("sql_blocked", f"blocked by SQL guard: {exc.reason}")
        except sqlite3.Error as exc:
            return _missing("query_failed", f"query failed: {exc}")
        return ToolResult.json(
            {
                "columns": columns,
                "rows": [dict(r) for r in rows],
                "row_count": len(rows),
                "tables": plan.tables,
                "guard": {"limited": plan.limited, "warnings": plan.warnings},
                "executed_at": now_iso(),
            }
        )

    @server.tool(
        "game_kpi_summary",
        description=(
            "Summarise game KPIs (spend, installs, CPI, ROAS) by channel for a date "
            "range. Fixed parameterised query — prefer this over run_sql."
        ),
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "game_id": {"type": "string", "maxLength": 64},
                "since": {"type": "string", "maxLength": 32},
                "until": {"type": "string", "maxLength": 32},
            },
        },
    )
    def game_kpi_summary(args: dict[str, Any]) -> ToolResult:
        clauses = ["1=1"]
        params: list[Any] = []
        for column, param_key in (("game_id", "game_id"), ("metric_date", "since"),
                                  ("metric_date", "until")):
            if args.get(param_key):
                operator = ">=" if param_key == "since" else ("<=" if param_key == "until"
                                                               else "=")
                clauses.append(f"{column} {operator} ?")
                params.append(str(args[param_key]))
        sql = (
            "SELECT channel, game_id, COUNT(*) AS rows, SUM(spend_usd) AS spend_usd, "
            "SUM(installs) AS installs, "
            "AVG(CASE WHEN installs > 0 THEN spend_usd / installs END) AS avg_cpi, "
            "SUM(revenue_usd) AS revenue_usd, "
            "CASE WHEN SUM(spend_usd) > 0 THEN SUM(revenue_usd) / SUM(spend_usd) "
            "END AS roas FROM marketing_metrics WHERE " + " AND ".join(clauses) +
            " GROUP BY channel, game_id ORDER BY spend_usd DESC"
        )
        try:
            with _connect() as connection:
                if "marketing_metrics" not in _user_tables(connection):
                    return _missing("table_missing",
                                    "marketing_metrics is not in the warehouse yet")
                rows = connection.execute(sql, params).fetchall()
        except FileNotFoundError as exc:
            return _missing("warehouse_missing", str(exc))
        except sqlite3.Error as exc:
            return _missing("query_failed", f"query failed: {exc}")
        return ToolResult.json(
            {"filters": args, "rows": [dict(r) for r in rows], "row_count": len(rows)}
        )

    return server


