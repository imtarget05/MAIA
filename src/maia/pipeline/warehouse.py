"""Mini warehouse for marketing / product data (SQLite, star-ish schema).

Why SQLite: the point of this layer is a *durable, queryable* landing zone that
works offline in tests, in CI and on a laptop — not a data platform. The schema is
close to what a real warehouse would use (typed columns, composite keys, an
ingest-run ledger) so moving to Postgres/DuckDB later is a driver change, not a
model change.

Two decisions that show up everywhere downstream:

* **Idempotent upserts.** ``reviews`` is keyed by a content-derived ``review_id``
  and ``marketing_metrics`` by ``(metric_date, game_id, channel, campaign_id)``.
  Re-running an ingestion updates in place instead of duplicating — otherwise a
  retried pipeline silently inflates every KPI.
* **Every run is recorded** in ``ingest_runs`` with in/new/updated/rejected
  counts. "How many rows did yesterday's job actually add?" is a question an
  operator must be able to answer without grepping logs.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..sql_guard import guard

__all__ = ["SCHEMA", "MarketWarehouse", "read_only_tables"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id   TEXT PRIMARY KEY,
    game_id     TEXT NOT NULL,
    source      TEXT NOT NULL,
    locale      TEXT NOT NULL DEFAULT '',
    rating      INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    review_date TEXT,
    sentiment   TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_game_date ON reviews(game_id, review_date);
CREATE INDEX IF NOT EXISTS idx_reviews_sentiment ON reviews(sentiment);

CREATE TABLE IF NOT EXISTS marketing_metrics (
    metric_date   TEXT NOT NULL,
    game_id       TEXT NOT NULL,
    channel       TEXT NOT NULL,
    campaign_id   TEXT NOT NULL DEFAULT '',
    impressions   INTEGER NOT NULL DEFAULT 0,
    clicks        INTEGER NOT NULL DEFAULT 0,
    installs      INTEGER NOT NULL DEFAULT 0,
    spend_usd     REAL NOT NULL DEFAULT 0,
    revenue_usd   REAL NOT NULL DEFAULT 0,
    retention_d1  REAL,
    logins        INTEGER,
    ingested_at   TEXT NOT NULL,
    PRIMARY KEY (metric_date, game_id, channel, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_metrics_game_date ON marketing_metrics(game_id, metric_date);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    rows_in       INTEGER NOT NULL DEFAULT 0,
    rows_new      INTEGER NOT NULL DEFAULT 0,
    rows_updated  INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS source_watermarks (
    source    TEXT PRIMARY KEY,
    value     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

REVIEW_TABLES = frozenset({"reviews"})
METRIC_TABLES = frozenset({"marketing_metrics", "reviews"})
USER_TABLES = frozenset({"reviews", "marketing_metrics"})


def read_only_tables() -> frozenset[str]:
    """Tables an analyst may query (excludes the ingest bookkeeping tables)."""
    return USER_TABLES


class MarketWarehouse:
    """Thin data-access layer over the warehouse (writes + guarded reads)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ---- connection ------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self.session() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    # ---- reads -----------------------------------------------------------
    def query(
        self, sql: str, params: Iterable[Any] = (), *, max_rows: int = 1000
    ) -> list[dict[str, Any]]:
        """Run a guarded read-only query and return dict rows.

        Guarded on purpose: this is the same entry point the MCP SQL tool uses, so
        an internal caller cannot exceed the policy an external one is held to.
        """
        plan = guard(sql, allowed_tables=USER_TABLES, max_rows=max_rows)
        with self.session() as connection:
            rows = connection.execute(plan.sql, list(params)).fetchall()
        return [dict(r) for r in rows]

    def row_counts(self) -> dict[str, int]:
        with self.session() as connection:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                )
                for table in sorted(USER_TABLES)
            }

    def watermark(self, source: str) -> str | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT value FROM source_watermarks WHERE source = ?", (source,)
            ).fetchone()
        return row["value"] if row else None

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Operator view of the ingest ledger.

        Reads ``ingest_runs`` directly rather than through :meth:`query`: the
        ledger is intentionally *not* analyst-queryable (``query`` enforces the
        table allowlist), but "what did the last run do?" must still be
        answerable without a shell.
        """
        with self.session() as connection:
            rows = connection.execute(
                "SELECT run_id, source, kind, rows_in, rows_new, rows_updated,"
                " rows_rejected, started_at, finished_at, note FROM ingest_runs"
                " ORDER BY started_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- writes ----------------------------------------------------------
    def upsert_reviews(
        self, records: list[dict[str, Any]], *, ingested_at: str
    ) -> dict[str, int]:
        """Insert/update reviews. Returns ``{"new": n, "updated": n}``."""
        counts = {"new": 0, "updated": 0}
        if not records:
            return counts
        with self.session() as connection:
            for record in records:
                exists = connection.execute(
                    "SELECT 1 FROM reviews WHERE review_id = ?", (record["review_id"],)
                ).fetchone()
                connection.execute(
                    "INSERT INTO reviews (review_id, game_id, source, locale, rating,"
                    " title, body, review_date, sentiment, ingested_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(review_id) DO UPDATE SET rating=excluded.rating,"
                    " body=excluded.body, title=excluded.title,"
                    " sentiment=excluded.sentiment, ingested_at=excluded.ingested_at",
                    (
                        record["review_id"], record["game_id"], record["source"],
                        record.get("locale", ""), int(record["rating"]),
                        record.get("title", ""), record["body"],
                        record.get("review_date"), record.get("sentiment"), ingested_at,
                    ),
                )
                counts["updated" if exists else "new"] += 1
            connection.commit()
        return counts

    def upsert_metrics(
        self, records: list[dict[str, Any]], *, ingested_at: str
    ) -> dict[str, int]:
        """Insert/update marketing metrics on the composite key."""
        counts = {"new": 0, "updated": 0}
        if not records:
            return counts
        with self.session() as connection:
            for record in records:
                key = (
                    record["metric_date"], record["game_id"], record["channel"],
                    record.get("campaign_id", ""),
                )
                exists = connection.execute(
                    "SELECT 1 FROM marketing_metrics WHERE metric_date=? AND game_id=?"
                    " AND channel=? AND campaign_id=?",
                    key,
                ).fetchone()
                connection.execute(
                    "INSERT INTO marketing_metrics (metric_date, game_id, channel,"
                    " campaign_id, impressions, clicks, installs, spend_usd, revenue_usd,"
                    " retention_d1, logins, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(metric_date, game_id, channel, campaign_id) DO UPDATE"
                    " SET impressions=excluded.impressions, clicks=excluded.clicks,"
                    " installs=excluded.installs, spend_usd=excluded.spend_usd,"
                    " revenue_usd=excluded.revenue_usd,"
                    " retention_d1=excluded.retention_d1, logins=excluded.logins,"
                    " ingested_at=excluded.ingested_at",
                    (
                        *key,
                        int(record.get("impressions") or 0),
                        int(record.get("clicks") or 0),
                        int(record.get("installs") or 0),
                        float(record.get("spend_usd") or 0.0),
                        float(record.get("revenue_usd") or 0.0),
                        record.get("retention_d1"),
                        record.get("logins"),
                        ingested_at,
                    ),
                )
                counts["updated" if exists else "new"] += 1
            connection.commit()
        return counts

    def set_sentiment(self, review_id: str, sentiment: str) -> None:
        with self.session() as connection:
            connection.execute(
                "UPDATE reviews SET sentiment = ? WHERE review_id = ?",
                (sentiment, review_id),
            )
            connection.commit()

    def set_watermark(self, source: str, value: str, *, updated_at: str) -> None:
        with self.session() as connection:
            connection.execute(
                "INSERT INTO source_watermarks (source, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(source) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (source, value, updated_at),
            )
            connection.commit()

    def record_run(
        self,
        *,
        run_id: str,
        source: str,
        kind: str,
        rows_in: int,
        rows_new: int,
        rows_updated: int,
        rows_rejected: int,
        started_at: str,
        finished_at: str,
        note: str = "",
    ) -> None:
        with self.session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO ingest_runs (run_id, source, kind, rows_in,"
                " rows_new, rows_updated, rows_rejected, started_at, finished_at, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, source, kind, rows_in, rows_new, rows_updated, rows_rejected,
                 started_at, finished_at, note),
            )
            connection.commit()


