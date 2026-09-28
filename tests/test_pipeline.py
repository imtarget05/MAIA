"""Data-pipeline tests: SQL guard, collectors, warehouse, analytics, NL-to-SQL.

The properties under test are the ones that break silently in production:
idempotent re-ingestion, rejected-row accounting, read-only enforcement, safe
division, and — most importantly — that the NL-to-SQL path *refuses* rather than
guessing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maia.pipeline.analytics import (
    classify_sentiment,
    detect_kpi_drop,
    kpi_rollup,
    review_insight,
    topic_buckets,
)
from maia.pipeline.collectors import (
    ConnectorError,
    FileConnector,
    HttpJsonConnector,
    ingest_metrics,
    ingest_reviews,
    normalize_metric,
    normalize_review,
    review_id_for,
)
from maia.pipeline.nl2sql import plan_sql, rule_rules
from maia.pipeline.warehouse import MarketWarehouse
from maia.sql_guard import SQLGuardError, guard, strip_comments

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_REVIEWS = REPO_ROOT / "data" / "market" / "samples" / "game_reviews.jsonl"
SAMPLE_METRICS = REPO_ROOT / "data" / "market" / "samples" / "marketing_metrics.csv"
ALLOWED = {"reviews", "marketing_metrics"}


# --------------------------------------------------------------------------- #
# SQL guard
# --------------------------------------------------------------------------- #
def test_strip_comments_removes_both_styles():
    assert strip_comments("SELECT 1 -- trailing") == "SELECT 1"
    assert "secret" not in strip_comments("SELECT /* secret */ 1")


@pytest.mark.parametrize(
    "sql,fragment",
    [
        # A statement that is not a SELECT is rejected before keyword analysis, so
        # the reason is the read-only rule; DDL/DML *inside* a SELECT is caught by
        # the keyword scan below.
        ("DROP TABLE reviews", "only SELECT / WITH"),
        ("DELETE FROM reviews", "only SELECT / WITH"),
        ("UPDATE reviews SET rating = 5", "only SELECT / WITH"),
        ("PRAGMA table_info(reviews)", "only SELECT / WITH"),
        ("ATTACH DATABASE 'x.db' AS x", "only SELECT / WITH"),
        ("WITH x AS (DELETE FROM reviews RETURNING *) SELECT * FROM x", "DELETE"),
        ("SELECT 1; DELETE FROM reviews", "multiple statements"),
        ("SELECT load_extension('evil') FROM reviews", "forbidden function"),
    ],
)
def test_guard_blocks_dangerous_statements(sql, fragment):
    with pytest.raises(SQLGuardError) as exc:
        guard(sql, allowed_tables=ALLOWED)
    assert fragment in str(exc.value)


def test_guard_allows_select_and_appends_a_limit():
    plan = guard("SELECT * FROM reviews", allowed_tables=ALLOWED, max_rows=25)
    assert plan.sql == "SELECT * FROM reviews LIMIT 25"
    assert plan.tables == ["reviews"]
    assert plan.limited is True


def test_guard_clamps_an_oversized_limit_but_keeps_a_smaller_one():
    big = guard("SELECT * FROM reviews LIMIT 10000", allowed_tables=ALLOWED, max_rows=50)
    small = guard("SELECT * FROM reviews LIMIT 5", allowed_tables=ALLOWED, max_rows=50)
    assert big.sql.endswith("LIMIT 50") and big.limited is True
    assert small.sql.endswith("LIMIT 5") and small.limited is False


def test_guard_rejects_unknown_tables():
    with pytest.raises(SQLGuardError, match="allowlist"):
        guard("SELECT * FROM secrets", allowed_tables=ALLOWED)


def test_guard_allows_cte_names_that_are_not_tables():
    plan = guard(
        "WITH recent AS (SELECT * FROM reviews) SELECT * FROM recent",
        allowed_tables=ALLOWED,
    )
    assert plan.tables == ["reviews"]


def test_guard_records_a_warning_when_no_allowlist_is_given():
    plan = guard("SELECT * FROM reviews", allowed_tables=None)
    assert plan.warnings == ["table allowlist not enforced by caller"]


def test_guard_requires_a_from_clause():
    with pytest.raises(SQLGuardError, match="does not read from any table"):
        guard("SELECT 1", allowed_tables=ALLOWED)


# --------------------------------------------------------------------------- #
# Collectors / normalisation
# --------------------------------------------------------------------------- #
def test_review_id_is_content_addressed():
    a = review_id_for("play", "g1", "2026-09-20", 4, "great game")
    b = review_id_for("play", "g1", "2026-09-20", 4, "great game")
    c = review_id_for("play", "g1", "2026-09-21", 4, "great game")
    assert a == b and a != c


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ({"rating": 4}, "no body"),
        ({"body": "x", "rating": 0}, "out of range"),
        ({"body": "x", "rating": 9}, "out of range"),
        ({"body": "x", "rating": "abc"}, "not an integer"),
    ],
)
def test_normalize_review_rejects_unusable_records(raw, fragment):
    with pytest.raises(ValueError) as exc:
        normalize_review(raw, game_id="g1", source="play")
    assert fragment in str(exc.value)


def test_normalize_review_maps_alternative_field_names():
    record = normalize_review(
        {"text": "  hay  ", "score": 5, "created_at": "2026-09-20T10:00:00Z",
         "country": "VN"},
        game_id="g1", source="play",
    )
    assert record["body"] == "hay"
    assert record["rating"] == 5
    assert record["review_date"] == "2026-09-20"
    assert record["locale"] == "VN"


def test_normalize_metric_defaults_missing_counters_to_zero():
    record = normalize_metric({"date": "2026-09-20", "channel": "facebook"},
                              game_id="g1")
    assert record["impressions"] == 0
    assert record["spend_usd"] == 0.0
    assert record["metric_date"] == "2026-09-20"


@pytest.mark.parametrize("raw,fragment", [
    ({"channel": "facebook"}, "metric_date"),
    ({"date": "2026-09-20"}, "channel"),
    ({"date": "2026-09-20", "channel": "f"}, "game_id"),
    ({"date": "2026-09-20", "channel": "f", "game_id": "g", "spend_usd": "abc"},
     "not numeric"),
])
def test_normalize_metric_rejects_unusable_records(raw, fragment):
    with pytest.raises(ValueError) as exc:
        normalize_metric(raw)
    assert fragment in str(exc.value)


def test_file_connector_reads_every_supported_format(tmp_path):
    (tmp_path / "a.jsonl").write_text('{"rating": 5}\n{"rating": 4}\n', encoding="utf-8")
    (tmp_path / "a.csv").write_text("rating\n5\n4\n", encoding="utf-8")
    (tmp_path / "a.json").write_text('[{"rating": 3}]', encoding="utf-8")
    assert len(FileConnector(tmp_path / "a.jsonl").fetch()) == 2
    assert len(FileConnector(tmp_path / "a.csv").fetch()) == 2
    assert len(FileConnector(tmp_path / "a.json").fetch()) == 1


def test_file_connector_reports_missing_and_unsupported_files(tmp_path):
    with pytest.raises(ConnectorError, match="not found"):
        FileConnector(tmp_path / "nope.json").fetch()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConnectorError, match="unsupported fixture format"):
        FileConnector(tmp_path / "a.txt").fetch()


def test_file_connector_since_filter_uses_the_date_field():
    assert len(FileConnector(SAMPLE_REVIEWS, since="2026-09-25").fetch()) == 4


def test_http_connector_is_opt_in(monkeypatch):
    monkeypatch.setattr("maia.config.settings.MARKET_HTTP_ENABLED", False, raising=False)
    with pytest.raises(ConnectorError, match="opt-in"):
        HttpJsonConnector("https://example.invalid/api", source="x").fetch()


def test_http_connector_follows_cursors(monkeypatch):
    monkeypatch.setattr("maia.config.settings.MARKET_HTTP_ENABLED", True, raising=False)
    pages = [{"data": [{"rating": 5}], "next_cursor": "c2"}, {"data": [{"rating": 4}]}]

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return pages.pop(0)

    seen = []
    rows = HttpJsonConnector(
        "https://example.invalid/api", source="x",
        http_get=lambda url, headers=None, params=None, timeout=None: (
            seen.append(params) or _Response()
        ),
    ).fetch()
    assert len(rows) == 2
    assert seen[1] == {"cursor": "c2"}


def test_http_connector_reports_a_non_list_payload(monkeypatch):
    monkeypatch.setattr("maia.config.settings.MARKET_HTTP_ENABLED", True, raising=False)

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {"error": "quota exceeded"}

    with pytest.raises(ConnectorError, match="no recognisable item list"):
        HttpJsonConnector("https://example.invalid/api", source="x",
                          http_get=lambda *a, **k: _Response()).fetch()


# --------------------------------------------------------------------------- #
# Warehouse + ingestion
# --------------------------------------------------------------------------- #
@pytest.fixture
def warehouse(tmp_path):
    wh = MarketWarehouse(tmp_path / "market.db")
    wh.ensure_schema()
    return wh


def test_ingestion_is_idempotent_for_reviews(warehouse):
    first = ingest_reviews(FileConnector(SAMPLE_REVIEWS), warehouse, game_id="demo-game")
    assert first.ok is True
    assert first.rows_new == 10
    assert first.rows_rejected == 0

    second = ingest_reviews(FileConnector(SAMPLE_REVIEWS), warehouse, game_id="demo-game")
    assert second.rows_new == 0
    assert second.rows_updated == 10
    assert warehouse.row_counts()["reviews"] == 10


def test_ingestion_is_idempotent_for_metrics(warehouse):
    assert ingest_metrics(FileConnector(SAMPLE_METRICS), warehouse).ok is True
    assert warehouse.row_counts()["marketing_metrics"] == 24
    second = ingest_metrics(FileConnector(SAMPLE_METRICS), warehouse)
    assert second.rows_new == 0
    assert second.rows_updated == 24


def test_bad_rows_are_counted_not_swallowed(tmp_path):
    bad = tmp_path / "reviews.jsonl"
    bad.write_text(
        '{"rating": 5, "body": "ok row", "date": "2026-09-20"}\n'
        '{"rating": 5, "date": "2026-09-20"}\n'
        '{"rating": 42, "body": "impossible", "date": "2026-09-20"}\n',
        encoding="utf-8",
    )
    result = ingest_reviews(FileConnector(bad), MarketWarehouse(tmp_path / "m.db"),
                            game_id="g1")
    assert result.rows_in == 3
    assert result.rows_new == 1
    assert result.rows_rejected == 2
    assert any("out of range" in r["error"] for r in result.rejections)
    assert any("no body" in r["error"] for r in result.rejections)
    assert result.ok is True  # partial success is still a successful run


def test_a_source_returning_only_garbage_is_a_failed_run(tmp_path):
    bad = tmp_path / "reviews.jsonl"
    bad.write_text('{"rating": 99, "body": "x"}\n', encoding="utf-8")
    result = ingest_reviews(FileConnector(bad), MarketWarehouse(tmp_path / "m.db"),
                            game_id="g1")
    assert result.ok is False
    assert "all rows rejected" in result.note


def test_a_missing_source_is_recorded_in_the_ledger(tmp_path):
    warehouse = MarketWarehouse(tmp_path / "m.db")
    result = ingest_reviews(FileConnector(tmp_path / "absent.jsonl"), warehouse,
                            game_id="g1")
    assert result.ok is False
    assert "not found" in result.note
    runs = warehouse.recent_runs()
    assert runs and runs[0]["rows_in"] == 0
    assert "ConnectorError" in runs[0]["note"]


def test_duplicate_rows_inside_one_batch_are_dropped(tmp_path):
    duplicated = tmp_path / "dupes.jsonl"
    row = '{"rating": 5, "body": "same review", "date": "2026-09-20"}\n'
    duplicated.write_text(row * 2, encoding="utf-8")
    result = ingest_reviews(FileConnector(duplicated), MarketWarehouse(tmp_path / "m.db"),
                            game_id="g1")
    assert result.rows_new == 1
    assert any(r["error"] == "duplicate_in_batch" for r in result.rejections)


def test_watermark_advances_with_the_latest_review(tmp_path):
    warehouse = MarketWarehouse(tmp_path / "m.db")
    ingest_reviews(FileConnector(SAMPLE_REVIEWS), warehouse, game_id="demo-game",
                   source="play")
    assert warehouse.watermark("play") == "2026-09-26"


def test_warehouse_query_enforces_the_same_guard_as_the_mcp_tool(warehouse):
    from maia.sql_guard import SQLGuardError

    with pytest.raises(SQLGuardError):
        warehouse.query("DELETE FROM reviews")
    with pytest.raises(SQLGuardError):
        warehouse.query("SELECT * FROM ingest_runs")  # internal table
    assert warehouse.query("SELECT COUNT(*) AS n FROM reviews")[0]["n"] == 0


def test_warehouse_is_the_writer_so_direct_writes_are_allowed(warehouse):
    assert warehouse.query("SELECT * FROM reviews", max_rows=1) == []
    with warehouse.connect() as connection:
        connection.execute(
            "INSERT INTO reviews (review_id, game_id, source, rating, body, ingested_at)"
            " VALUES ('x','g','s',5,'body','now')"
        )
        connection.commit()
    assert warehouse.row_counts()["reviews"] == 1


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,rating,expected",
    [
        ("game hay, mượt, đẹp", 5, "positive"),
        ("lag khủng khiếp, lỗi liên tục", 1, "negative"),
        ("không hay lắm", 3, "negative"),  # negation flips the token
        ("bình thường", 1, "negative"),    # rating tie-breaks an ambiguous text
    ],
)
def test_classify_sentiment(text, rating, expected):
    assert classify_sentiment(text, rating)["label"] == expected


def test_classify_sentiment_reports_which_signal_decided():
    assert classify_sentiment("bình thường", 1)["decided_by"] == "rating"
    assert classify_sentiment("bình thường", 1)["method"] == "lexicon_v1"
    assert classify_sentiment("hay quá", None)["decided_by"] == "text"


def test_topic_buckets_keep_evidence_quotes():
    buckets = topic_buckets(
        [{"title": "", "body": "game lag khi vào trận, fps thấp"},
         {"title": "", "body": "đồng bộ lỗi liên tục, crash"},
         {"title": "", "body": "bản đồ mới rất đẹp"}]
    )
    themes = {b["theme"]: b for b in buckets}
    # A review can belong to several themes on purpose: "crash" is both a
    # performance and a stability signal, and hiding one of them would
    # under-report the problem. Counts are therefore per-theme, not per-review.
    assert themes["performance"]["count"] == 2
    assert themes["stability"]["count"] == 1
    assert themes["performance"]["quotes"]
    assert themes["stability"]["quotes"] == ["đồng bộ lỗi liên tục, crash"]
    assert any(b["theme"] == "content" for b in buckets)


def test_review_insight_is_explicit_about_method_and_sample():
    insight = review_insight(
        [{"rating": 1, "body": "lag"}, {"rating": 5, "body": "mượt"}],
        product_name="g1",
    )
    assert insight["total"] == 2
    assert insight["sentiment_breakdown"]["total"] == 2
    assert insight["method"] == "lexicon_v1"
    assert "not a trained classifier" in insight["caveats"][0]
    assert review_insight([])["caveats"] == ["no reviews in scope"]


def test_kpi_rollup_computes_ratios_and_survives_zero_denominators():
    rows = [
        {"channel": "facebook", "impressions": 1000, "clicks": 50, "installs": 10,
         "spend_usd": 100.0, "revenue_usd": 250.0},
        {"channel": "tiktok", "impressions": 0, "clicks": 0, "installs": 0,
         "spend_usd": 0.0, "revenue_usd": 0.0},
    ]
    rollup = {r["channel"]: r for r in kpi_rollup(rows)}
    assert rollup["facebook"]["ctr"] == pytest.approx(0.05)
    assert rollup["facebook"]["cpi"] == pytest.approx(10.0)
    assert rollup["facebook"]["roas"] == pytest.approx(2.5)
    assert rollup["tiktok"]["ctr"] is None  # no ZeroDivisionError, no NaN


def _series(values, start_day=1):
    return [{"metric_date": f"2026-09-{start_day + i:02d}", "retention_d1": v}
            for i, v in enumerate(values)]


def test_detect_kpi_drop_needs_both_signals():
    assert detect_kpi_drop(_series([0.42, 0.42, 0.41, 0.42]))["alert"] is False
    dropped = detect_kpi_drop(_series([0.42, 0.41, 0.42, 0.29]))
    assert dropped["alert"] is True
    assert dropped["severity"] in {"high", "critical"}
    assert dropped["signals"] == {"week_over_week_drop": True, "outlier": True}
    assert dropped["zscore"] < -2


def test_detect_kpi_drop_ignores_short_history():
    result = detect_kpi_drop(_series([0.42, 0.10]))
    assert result["alert"] is False
    assert result["reason"] == "insufficient_history"


def test_detect_kpi_drop_averages_duplicate_dates():
    """Per-channel rows on the same date must not create a fake 46% swing."""
    rows = _series([0.42, 0.42, 0.42])
    rows += [{"metric_date": rows[-1]["metric_date"], "retention_d1": 0.40}]
    result = detect_kpi_drop(rows)
    assert result["aggregated_dates"] is True
    assert abs(result["pct_change"]) < 5.0


def test_severity_ladder_matches_the_prompt_thresholds():
    from maia.pipeline.analytics import severity_for

    assert severity_for(-3.0) == "low"
    assert severity_for(-6.0) == "medium"
    assert severity_for(-16.0) == "high"
    assert severity_for(-30.0) == "critical"


# --------------------------------------------------------------------------- #
# NL-to-SQL
# --------------------------------------------------------------------------- #
SCHEMA = {
    "marketing_metrics": {"metric_date", "game_id", "channel", "campaign_id",
                          "impressions", "clicks", "installs", "spend_usd",
                          "revenue_usd", "retention_d1", "logins"},
    "reviews": {"review_id", "game_id", "source", "locale", "rating", "title",
                "body", "review_date", "sentiment"},
}


@pytest.mark.parametrize(
    "question,rule_id",
    [
        ("CPI trung bình theo kênh là bao nhiêu?", "cpi_by_channel"),
        ("ROAS tháng này thế nào?", "roas_by_channel"),
        ("CTR đang ra sao?", "ctr_by_channel"),
        ("chuỗi retention d1 của game", "retention_series"),
        ("cảm xúc review như thế nào?", "review_sentiment_breakdown"),
        ("review 1 sao mới nhất", "lowest_rated_reviews"),
    ],
)
def test_rules_match_vietnamese_questions(question, rule_id):
    plan = plan_sql(question, schema=SCHEMA)
    assert plan.supported is True
    assert plan.rule_id == rule_id
    assert plan.confidence == "high"
    assert plan.sql.upper().startswith("SELECT")


def test_rules_match_unaccented_questions():
    """Patterns are ASCII; folding makes both spellings work."""
    assert plan_sql("chi phi cai dat theo kenh", schema=SCHEMA).supported is True


def test_unsupported_question_refuses_instead_of_guessing():
    plan = plan_sql("Doanh thu theo quốc gia của người chơi là bao nhiêu?",
                    schema=SCHEMA)
    assert plan.supported is False
    assert plan.sql == ""
    assert plan.confidence == "low"
    assert plan.reason


def test_rule_refuses_when_the_schema_lacks_a_column():
    thin = {"marketing_metrics": {"metric_date", "game_id", "channel"}}
    plan = plan_sql("CPI theo kênh", schema=thin)
    assert plan.supported is False
    assert "spend_usd" in plan.reason


def test_llm_sql_is_accepted_only_when_its_columns_exist():
    good = plan_sql(
        "câu hỏi lạ", schema=SCHEMA,
        llm_sql="SELECT channel, SUM(installs) FROM marketing_metrics GROUP BY channel",
        llm_explanation="đếm lượt cài theo kênh",
    )
    assert good.supported is True
    assert good.path == "llm"

    hallucinated = plan_sql(
        "câu hỏi lạ", schema=SCHEMA,
        llm_sql="SELECT country FROM marketing_metrics",
    )
    assert hallucinated.supported is False
    assert hallucinated.rule_id == ""  # fell through to "no rule matched"


def test_rule_table_is_exposed_as_data():
    rules = rule_rules()
    assert {r["id"] for r in rules} >= {"cpi_by_channel", "lowest_rated_reviews"}
    assert all(r["explanation"] for r in rules)


def test_planned_sql_actually_runs_against_the_warehouse(warehouse):
    ingest_metrics(FileConnector(SAMPLE_METRICS), warehouse)
    plan = plan_sql("ROAS theo kênh", schema=SCHEMA)
    rows = warehouse.query(plan.sql, plan.params, max_rows=20)
    assert rows
    assert {"channel", "roas"} <= set(rows[0])



