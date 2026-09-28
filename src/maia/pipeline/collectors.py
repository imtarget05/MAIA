"""Collectors: bring game reviews / campaign metrics into the warehouse.

Connector design follows the repo's existing convention (``hris.py``,
``itsm.py``): a connector is a thin, injectable, **never-raising** boundary, and
failures are recorded in the run ledger instead of crashing a job.

Shipped connectors:

* :class:`FileConnector` — JSON / JSONL / CSV fixtures under ``MARKET_DATA_DIR``.
  This is the default and what CI uses: reproducible, no network, no scraping.
* :class:`HttpJsonConnector` — a real API/connector endpoint, opt-in via
  ``MARKET_HTTP_ENABLED``, with cursor pagination, bounded pages, per-request
  timeout and a caller-supplied row mapper. Off by default so a demo or a test
  can never start pulling live third-party data.

Normalisation is the part that matters for correctness:

* ``review_id`` is a content hash of (source, game, date, rating, body), so a
  review fetched twice is one row — upstream re-publishes constantly.
* Records that fail validation are **counted and reported**, never silently
  dropped: a source that suddenly returns HTML or changes schema must be visible
  in the run ledger.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..common import content_id, now_iso
from ..config import settings
from .warehouse import MarketWarehouse

__all__ = [
    "CollectedRecords",
    "ConnectorError",
    "FileConnector",
    "HttpJsonConnector",
    "IngestResult",
    "ingest_metrics",
    "ingest_reviews",
    "normalize_metric",
    "normalize_review",
    "review_id_for",
]

MAX_BODY_CHARS = 4000


class ConnectorError(RuntimeError):
    """A connector could not deliver records (network, schema, permissions)."""


@dataclass
class CollectedRecords:
    """Validated records plus a per-record rejection report."""

    records: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""

    @property
    def accepted_count(self) -> int:
        return len(self.records)


@dataclass
class IngestResult:
    """Outcome of one ingestion run (stored in ``ingest_runs`` and reported)."""

    ok: bool
    source: str
    kind: str
    run_id: str
    rows_in: int = 0
    rows_new: int = 0
    rows_updated: int = 0
    rows_rejected: int = 0
    rejections: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "source": self.source, "kind": self.kind,
            "run_id": self.run_id, "rows_in": self.rows_in, "rows_new": self.rows_new,
            "rows_updated": self.rows_updated, "rows_rejected": self.rows_rejected,
            "note": self.note, "started_at": self.started_at,
            "finished_at": self.finished_at, "rejections": self.rejections[:20],
        }


def review_id_for(source: str, game_id: str, review_date: str | None, rating: Any,
                  body: str) -> str:
    """Content-addressed review id (the dedupe key)."""
    blob = "|".join([source, game_id, str(review_date or ""), str(rating), body[:200]])
    return "rv_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _clean_text(value: Any, limit: int = MAX_BODY_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_review(raw: dict[str, Any], *, game_id: str, source: str) -> dict[str, Any]:
    """Map a source-shaped review into the warehouse contract.

    Raises ``ValueError`` for records that cannot be repaired (missing body, rating
    outside 1-5) so the caller counts them as rejected — silently coercing a 0-star
    review would corrupt every sentiment metric downstream.
    """
    body = _clean_text(raw.get("body") or raw.get("text") or raw.get("comment"))
    if not body:
        raise ValueError("review has no body/text/comment")
    try:
        rating = int(raw.get("rating") or raw.get("score") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"review rating is not an integer: {raw.get('rating')!r}") from exc
    if not 1 <= rating <= 5:
        raise ValueError(f"review rating out of range 1-5: {rating}")
    review_date = str(
        raw.get("date") or raw.get("review_date") or raw.get("created_at") or ""
    )[:10]
    resolved_game = str(raw.get("game_id") or raw.get("game") or game_id)
    return {
        "review_id": review_id_for(source, resolved_game, review_date, rating, body),
        "game_id": resolved_game,
        "source": source,
        "locale": _clean_text(raw.get("locale") or raw.get("country") or "", 16),
        "rating": rating,
        "title": _clean_text(raw.get("title"), 200),
        "body": body,
        "review_date": review_date or None,
    }


_NUMERIC_FIELDS = {
    "impressions": int, "clicks": int, "installs": int, "logins": int,
    "spend_usd": float, "revenue_usd": float, "retention_d1": float,
}


def normalize_metric(
    raw: dict[str, Any], *, game_id: str = "", channel: str = ""
) -> dict[str, Any]:
    """Map a source-shaped metric row into the warehouse contract."""
    metric_date = str(raw.get("metric_date") or raw.get("date") or "")[:10]
    if not metric_date:
        raise ValueError("metric row has no metric_date/date")
    resolved_channel = str(raw.get("channel") or channel)
    if not resolved_channel:
        raise ValueError("metric row has no channel")
    record: dict[str, Any] = {
        "metric_date": metric_date,
        "game_id": str(raw.get("game_id") or raw.get("game") or game_id),
        "channel": resolved_channel,
        "campaign_id": _clean_text(raw.get("campaign_id") or "", 80),
    }
    if not record["game_id"]:
        raise ValueError("metric row has no game_id")
    for field_name, caster in _NUMERIC_FIELDS.items():
        value = raw.get(field_name)
        if value in (None, ""):
            record[field_name] = None
            continue
        try:
            record[field_name] = caster(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} is not numeric: {value!r}") from exc
    for field_name in ("impressions", "clicks", "installs"):
        if record.get(field_name) is None:
            record[field_name] = 0
    for field_name in ("spend_usd", "revenue_usd"):
        if record.get(field_name) is None:
            record[field_name] = 0.0
    return record


class Connector(Protocol):  # pragma: no cover - typing only
    name: str

    def fetch(self) -> list[dict[str, Any]]: ...


class FileConnector:
    """Reads records from a JSON / JSONL / CSV file (the offline default)."""

    name = "file"

    def __init__(self, path: str | Path, *, source: str = "", since: str = ""
                 ) -> None:
        self.path = Path(path)
        self.source = source or self.path.stem
        self.since = since

    def fetch(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise ConnectorError(f"fixture not found: {self.path}")
        suffix = self.path.suffix.lower()
        text = self.path.read_text(encoding="utf-8")
        if suffix == ".csv":
            rows: list[Any] = list(csv.DictReader(io.StringIO(text)))
        elif suffix in (".jsonl", ".ndjson"):
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        elif suffix == ".json":
            payload = json.loads(text)
            rows = payload if isinstance(payload, list) else payload.get("data") or []
        else:
            raise ConnectorError(f"unsupported fixture format: {suffix}")
        if self.since:
            rows = [
                r for r in rows
                if isinstance(r, dict)
                and str(r.get("date") or r.get("metric_date")
                        or r.get("review_date") or "")[:10] >= self.since
            ]
        return [r for r in rows if isinstance(r, dict)]


class HttpJsonConnector:
    """Fetches paginated JSON from an HTTP endpoint (opt-in, bounded).

    ``httpx`` is imported lazily and the payload is only trusted when it really
    is a JSON item list (or ``{"data": [...]}``): a captive portal or an error
    page must fail the run rather than be ingested as data.
    """

    name = "http"

    def __init__(
        self,
        url: str,
        *,
        source: str,
        headers: dict[str, str] | None = None,
        page_param: str = "cursor",
        items_path: Callable[[Any], list[dict]] | None = None,
        timeout: float | None = None,
        max_pages: int | None = None,
        http_get: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.source = source
        self.headers = headers or {}
        self.page_param = page_param
        self.items_path = items_path
        self.timeout = timeout if timeout is not None else settings.MARKET_HTTP_TIMEOUT_SEC
        self.max_pages = (
            max_pages if max_pages is not None else settings.MARKET_HTTP_MAX_PAGES
        )
        self._http_get = http_get

    def fetch(self) -> list[dict[str, Any]]:
        if not settings.MARKET_HTTP_ENABLED:
            raise ConnectorError(
                "MARKET_HTTP_ENABLED is false: remote collection is opt-in "
                "(offline fixtures are the supported path for tests and demos)"
            )
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max(1, self.max_pages)):
            params: dict[str, Any] = {self.page_param: cursor} if cursor else {}
            response = self._get(params)
            if response.status_code >= 400:
                raise ConnectorError(
                    f"HTTP {response.status_code} from {self.url}: {response.text[:200]}"
                )
            payload = response.json()
            items = self.items_path(payload) if self.items_path else self._default_items(
                payload
            )
            rows.extend(r for r in items if isinstance(r, dict))
            cursor = self._next_cursor(payload)
            if not cursor:
                break
        return rows

    def _get(self, params: dict[str, Any]) -> Any:
        if self._http_get:
            return self._http_get(
                self.url, headers=self.headers, params=params, timeout=self.timeout
            )
        import httpx

        return httpx.get(
            self.url, headers=self.headers, params=params, timeout=self.timeout
        )

    @staticmethod
    def _default_items(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            for key in ("data", "items", "results", "reviews"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [v for v in value if isinstance(v, dict)]
        raise ConnectorError("response JSON has no recognisable item list")

    @staticmethod
    def _next_cursor(payload: Any) -> str | None:
        if isinstance(payload, dict):
            cursor = payload.get("next_cursor") or payload.get("nextCursor")
            if cursor:
                return str(cursor)
        return None


def _now() -> str:
    """Indirection kept so tests can reason about "the run's clock" in one place."""
    return now_iso()


def _ingest(
    *,
    warehouse: MarketWarehouse,
    connector: Connector,
    kind: str,
    game_id: str,
    normalize: Callable[[dict[str, Any]], dict[str, Any]],
    source: str,
    max_rows: int,
) -> IngestResult:
    """Shared run loop: fetch → normalise → upsert → ledger.

    Everything is counted, and a connector-level failure is still written to
    ``ingest_runs`` (``ok=false``) so a broken source leaves evidence.
    """
    started = _now()
    run_id = content_id("run", kind, source, started)
    rows: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    note = ""
    ok = True
    try:
        rows = connector.fetch()
    except (ConnectorError, OSError, ValueError) as exc:
        ok = False
        note = f"{type(exc).__name__}: {exc}"
        rows = []

    normalized: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for raw in rows[: max(1, max_rows)]:
        try:
            record = normalize(raw)
        except ValueError as exc:
            rejections.append({"error": str(exc), "record": str(raw)[:200]})
            continue
        # Reviews dedupe on their content hash; metrics on their composite key.
        key: Any = record.get("review_id") or (
            record.get("metric_date"), record.get("game_id"),
            record.get("channel"), record.get("campaign_id"),
        )
        if key in seen:  # intra-batch duplicate
            rejections.append({"error": "duplicate_in_batch", "record": str(key)})
            continue
        seen.add(key)
        normalized.append(record)

    ingested_at = _now()
    counts = {"new": 0, "updated": 0}
    if normalized:
        if kind == "reviews":
            counts = warehouse.upsert_reviews(normalized, ingested_at=ingested_at)
        else:
            counts = warehouse.upsert_metrics(normalized, ingested_at=ingested_at)

    result = IngestResult(
        ok=ok,
        source=source,
        kind=kind,
        run_id=run_id,
        rows_in=len(rows),
        rows_new=counts["new"],
        rows_updated=counts["updated"],
        rows_rejected=len(rejections),
        rejections=rejections,
        note=note,
        started_at=started,
        finished_at=_now(),
    )
    if rows and not normalized and not note:
        # Everything the source returned was unusable — that is a failed run even
        # though the fetch "succeeded", and the operator must see it as such.
        result.ok = False
        result.note = "all rows rejected by validation"
    warehouse.record_run(
        run_id=run_id, source=source, kind=kind, rows_in=result.rows_in,
        rows_new=result.rows_new, rows_updated=result.rows_updated,
        rows_rejected=result.rows_rejected, started_at=result.started_at,
        finished_at=result.finished_at, note=note,
    )
    if normalized and kind == "reviews":
        latest = max(
            (r.get("review_date") or "" for r in normalized), default=""
        )
        if latest:
            warehouse.set_watermark(source, latest, updated_at=_now())
    return result


def ingest_reviews(
    connector: Connector,
    warehouse: MarketWarehouse,
    *,
    game_id: str,
    source: str = "",
    max_rows: int | None = None,
) -> IngestResult:
    """Ingest player reviews through ``connector`` into the warehouse."""
    warehouse.ensure_schema()
    resolved_source = source or getattr(connector, "source", "reviews")
    return _ingest(
        warehouse=warehouse,
        connector=connector,
        kind="reviews",
        game_id=game_id,
        normalize=lambda raw: normalize_review(raw, game_id=game_id, source=resolved_source),
        source=resolved_source,
        max_rows=max_rows or settings.MARKET_MAX_ROWS,
    )


def ingest_metrics(
    connector: Connector,
    warehouse: MarketWarehouse,
    *,
    game_id: str = "",
    source: str = "",
    max_rows: int | None = None,
) -> IngestResult:
    """Ingest campaign metrics through ``connector`` into the warehouse."""
    warehouse.ensure_schema()
    resolved_source = source or getattr(connector, "source", "metrics")
    return _ingest(
        warehouse=warehouse,
        connector=connector,
        kind="metrics",
        game_id=game_id,
        normalize=lambda raw: normalize_metric(raw, game_id=game_id),
        source=resolved_source,
        max_rows=max_rows or settings.MARKET_MAX_ROWS,
    )



