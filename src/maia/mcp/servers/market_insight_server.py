"""Market insight MCP server — ingestion and analysis as agent-callable tools.

This server turns the offline pipeline (:mod:`maia.pipeline`) into something an
agent can use: ingest a fixture of reviews, run lexicon/topic analysis, ask the
NL-to-SQL planner, and get a KPI-drop verdict.

Two honesty guards are deliberate:

* **Analysis labels its method.** Results carry ``method: "lexicon_v1"`` so an
  agent cannot present a lexicon pass as a trained classifier.
* **Ingestion is fixture-scoped.** ``ingest_*`` takes a path *inside* the
  configured data directory and refuses traversal, so a tool call (usually issued
  by an LLM) cannot reach arbitrary files — and every run is recorded in the
  warehouse's ``ingest_runs`` ledger.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config import settings
from ...pipeline.analytics import detect_kpi_drop, kpi_rollup, review_insight
from ...pipeline.collectors import FileConnector, ingest_metrics, ingest_reviews
from ...pipeline.nl2sql import plan_sql, rule_rules
from ...pipeline.warehouse import MarketWarehouse
from ..protocol import ToolResult
from ..server import MCPServer

__all__ = ["build_market_insight_server", "live_schema"]

MAX_ROWS = 5000


def _resolve_fixture(relative: str, data_dir: Path) -> Path:
    """Resolve a fixture inside the data dir, refusing path traversal.

    The caller is usually an LLM, so ``../../etc/passwd`` must be impossible by
    construction rather than by hoping the model stays well-behaved.
    """
    root = data_dir.resolve()
    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root)):
        raise ValueError("path escapes the configured market data directory")
    if not candidate.exists():
        raise FileNotFoundError(f"fixture not found under {root}: {relative}")
    return candidate


def live_schema(warehouse: MarketWarehouse) -> dict[str, set[str]]:
    """Read the warehouse's live table/column schema (for NL-to-SQL verification)."""
    try:
        with warehouse.session() as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            schema: dict[str, set[str]] = {}
            for row in tables:
                name = str(row["name"])
                columns = connection.execute(f"PRAGMA table_info({name})").fetchall()
                schema[name] = {str(c["name"]) for c in columns}
        return schema
    except Exception:
        return {}


def build_market_insight_server(
    *, db_path: str | Path | None = None, data_dir: str | Path | None = None
) -> MCPServer:
    """Build the ``market_insight`` server (ingest + analyse)."""
    warehouse = MarketWarehouse(db_path or settings.MARKET_DB_PATH)
    root = Path(data_dir or settings.MARKET_DATA_DIR)
    server = MCPServer(
        "market_insight",
        title="Game review & campaign insight",
        instructions=(
            "Ingest review/metric fixtures from the configured data directory and "
            "analyse them. Sentiment is lexicon-based (method=lexicon_v1)."
        ),
    )

    @server.tool(
        "ingest_reviews",
        description="Ingest a JSON/JSONL/CSV file of player reviews into the warehouse.",
        read_only=False,
        destructive=False,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["path", "game_id"],
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 400},
                "game_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "source": {"type": "string", "maxLength": 64},
            },
        },
    )
    def ingest_reviews_tool(args: dict[str, Any]) -> ToolResult:
        try:
            path = _resolve_fixture(args["path"], root)
        except (ValueError, FileNotFoundError) as exc:
            return ToolResult.text(str(exc), structured={"error": "bad_fixture"},
                                   is_error=True)
        result = ingest_reviews(
            FileConnector(path, source=args.get("source") or path.stem),
            warehouse,
            game_id=args["game_id"],
        )
        return ToolResult.json(result.to_dict(), is_error=not result.ok)

    @server.tool(
        "ingest_metrics",
        description="Ingest a CSV/JSON file of campaign metrics into the warehouse.",
        read_only=False,
        destructive=False,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["path"],
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 400},
                "source": {"type": "string", "maxLength": 64},
            },
        },
    )
    def ingest_metrics_tool(args: dict[str, Any]) -> ToolResult:
        try:
            path = _resolve_fixture(args["path"], root)
        except (ValueError, FileNotFoundError) as exc:
            return ToolResult.text(str(exc), structured={"error": "bad_fixture"},
                                   is_error=True)
        result = ingest_metrics(
            FileConnector(path, source=args.get("source") or path.stem), warehouse
        )
        return ToolResult.json(result.to_dict(), is_error=not result.ok)

    @server.tool(
        "review_sentiment_summary",
        description=(
            "Lexicon sentiment + topic buckets for a game's reviews "
            "(lexicon_v1; not a trained classifier)."
        ),
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["game_id"],
            "additionalProperties": False,
            "properties": {
                "game_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "min_rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            },
        },
    )
    def review_sentiment_summary(args: dict[str, Any]) -> ToolResult:
        sql = ("SELECT rating, title, body, review_date, sentiment FROM reviews "
               "WHERE game_id = ?")
        params: list[Any] = [args["game_id"]]
        if args.get("min_rating"):
            sql += " AND rating >= ?"
            params.append(int(args["min_rating"]))
        sql += (" ORDER BY review_date DESC LIMIT "
                f"{min(int(args.get('limit', 500)), MAX_ROWS)}")
        rows = warehouse.query(sql, params)
        payload = review_insight(rows, product_name=args["game_id"])
        payload["game_id"] = args["game_id"]
        payload["sample_size"] = len(rows)
        return ToolResult.json(payload)

    @server.tool(
        "campaign_performance",
        description="Roll up campaign KPIs (CTR, CPC, CPI, ROAS) by channel.",
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "game_id": {"type": "string", "maxLength": 64},
            },
        },
    )
    def campaign_performance(args: dict[str, Any]) -> ToolResult:
        sql = "SELECT * FROM marketing_metrics"
        params: list[Any] = []
        if args.get("game_id"):
            sql += " WHERE game_id = ?"
            params.append(args["game_id"])
        rows = warehouse.query(sql, params, max_rows=MAX_ROWS)
        return ToolResult.json(
            {"rows_in_scope": len(rows), "channels": kpi_rollup(rows), "filters": args}
        )

    @server.tool(
        "retention_drop_check",
        description=(
            "Check the retention D1 series for a statistically notable drop "
            "(week-over-week + z-score). Returns the verdict; sending the alert is a "
            "separate step."
        ),
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["game_id"],
            "additionalProperties": False,
            "properties": {
                "game_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "metric": {
                    "type": "string",
                    "enum": ["retention_d1", "logins", "revenue_usd"],
                },
            },
        },
    )
    def retention_drop_check(args: dict[str, Any]) -> ToolResult:
        # Aggregated per date: one point per day is what the detector expects, and
        # averaging here keeps the verdict correct when several channels report
        # for the same game on the same day.
        rows = warehouse.query(
            "SELECT metric_date, AVG(retention_d1) AS retention_d1, "
            "SUM(logins) AS logins, SUM(revenue_usd) AS revenue_usd "
            "FROM marketing_metrics WHERE game_id = ? AND retention_d1 IS NOT NULL "
            "GROUP BY metric_date ORDER BY metric_date",
            [args["game_id"]],
            max_rows=MAX_ROWS,
        )
        return ToolResult.json(
            detect_kpi_drop(
                rows, metric=args.get("metric", "retention_d1"), date_key="metric_date"
            )
        )

    @server.tool(
        "nl_to_sql",
        description=(
            "Plan a read-only query for a business question with the reviewed rule "
            "table. Refuses instead of guessing when nothing matches."
        ),
        read_only=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["question"],
            "additionalProperties": False,
            "properties": {
                "question": {"type": "string", "minLength": 3, "maxLength": 400}
            },
        },
    )
    def nl_to_sql_tool(args: dict[str, Any]) -> ToolResult:
        plan = plan_sql(args["question"], schema=live_schema(warehouse))
        payload = plan.to_dict()
        payload["available_rules"] = rule_rules()
        if plan.supported:
            payload["rows"] = warehouse.query(plan.sql, plan.params, max_rows=200)
        return ToolResult.json(payload, is_error=not plan.supported)

    return server


