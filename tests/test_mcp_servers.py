"""Integration-server tests: Airtable, notifications, SQL analytics, market insight.

Each test pins a *behaviour that matters operationally*, not just the happy path:

* Airtable — idempotent writes (a retried agent turn must not duplicate rows) and
  a remote-mode request shape that can be asserted without the network.
* Notifications — a dry run must never report ``delivered``, Teams' "200 +
  Invalid webhook" trap must be treated as a failure, and Zalo API errors must
  surface instead of being swallowed.
* SQL analytics — writes/traversal are blocked by the guard, the connection is
  read-only, and a missing warehouse is a clean error rather than a traceback.
* Market insight — path traversal in a tool argument is refused, ingestion is
  recorded in the ledger, and sentiment results are labelled with their method.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from maia.mcp.client import MCPClient
from maia.mcp.servers import build_server
from maia.mcp.servers.airtable_server import (
    LocalAirtableBackend,
    RemoteAirtableBackend,
    build_airtable_server,
)
from maia.mcp.servers.market_insight_server import build_market_insight_server
from maia.mcp.servers.notification_server import (
    NotificationDispatcher,
    build_notification_server,
)
from maia.mcp.servers.sql_analytics_server import build_sql_analytics_server
from maia.mcp.transport import InProcessTransport

TASK = {"campaign_id": "mua-4", "channel": "facebook",
        "content": "Ban do moi da mo, moi chi huy tham gia ngay hom nay."}


def _client(server) -> MCPClient:
    client = MCPClient(server.info.name, InProcessTransport(server))
    client.connect()
    return client


# --------------------------------------------------------------------------- #
# Airtable
# --------------------------------------------------------------------------- #
def test_airtable_create_is_idempotent_on_the_same_key(tmp_path):
    client = _client(build_airtable_server(
        backend=LocalAirtableBackend(tmp_path / "tasks.json")
    ))
    first = client.call_tool("create_marketing_task", TASK)
    second = client.call_tool("create_marketing_task", TASK)
    assert first["ok"] and second["ok"]
    assert first["structured"]["deduplicated"] is False
    assert second["structured"]["deduplicated"] is True
    assert first["structured"]["external_key"] == second["structured"]["external_key"]
    assert len(json.loads((tmp_path / "tasks.json").read_text())) == 1


def test_airtable_lists_and_updates_status(tmp_path):
    client = _client(build_airtable_server(
        backend=LocalAirtableBackend(tmp_path / "t.json")
    ))
    key = client.call_tool("create_marketing_task", TASK)["structured"]["external_key"]
    assert client.call_tool("list_content_calendar", {})["structured"]["count"] == 1

    updated = client.call_tool("update_task_status",
                               {"external_key": key, "status": "published"})
    assert updated["structured"]["status"] == "published"

    missing = client.call_tool("update_task_status",
                               {"external_key": "nope", "status": "published"})
    assert missing["ok"] is False
    assert missing["structured"]["error"] == "task_not_found"


def test_airtable_rejects_an_unknown_channel():
    client = _client(build_airtable_server(
        backend=LocalAirtableBackend("/tmp/unused-airtable.json")
    ))
    result = client.call_tool("create_marketing_task", {**TASK, "channel": "myspace"})
    assert result["ok"] is False
    assert "invalid arguments" in result["error"]


def test_remote_airtable_sends_the_expected_request():
    calls = {}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "rec123", "createdTime": "2026-09-28T00:00:00.000Z"}

    def _post(url, **kwargs):
        calls["url"] = url
        calls["json"] = kwargs.get("json")
        return _Response()

    backend = RemoteAirtableBackend(
        api_key="key-123", base_id="app123", table="Content Calendar", http_post=_post
    )
    result = backend.create_task({"external_key": "k1", "campaign_id": "c",
                                  "channel": "facebook", "content": "hello world"})
    assert calls["url"].startswith("https://api.airtable.com/v0/app123/")
    assert calls["json"]["fields"]["external_key"] == "k1"
    assert result["airtable_id"] == "rec123"
    assert result["storage"] == "airtable"
    assert backend.dry_run is False


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def test_email_without_smtp_is_a_visible_dry_run(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    client = _client(build_notification_server(
        NotificationDispatcher(outbox_path=outbox, dry_run=True)
    ))
    result = client.call_tool("send_email_report",
                              {"recipient": "lead@game.example", "subject": "Bao cao tuan",
                               "body": "Retention giam 3 diem."})
    assert result["ok"] is True  # the tool ran…
    structured = result["structured"]
    assert structured["dry_run"] is True
    assert structured["delivered"] is False  # …but nothing was delivered
    assert structured["reason"] == "dry_run"
    assert json.loads(outbox.read_text().splitlines()[0])["channel"] == "email"


def test_notification_dry_run_ids_are_content_addressed(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    client = _client(build_notification_server(
        NotificationDispatcher(outbox_path=outbox, dry_run=True)
    ))
    args = {"recipient": "lead@game.example", "subject": "Weekly", "body": "Body"}
    first = client.call_tool("send_email_report", args)["structured"]["delivery_id"]
    second = client.call_tool("send_email_report", args)["structured"]["delivery_id"]
    assert first == second  # a retry is the same delivery, not a new one


def test_teams_catches_the_invalid_webhook_body(tmp_path):
    dispatcher = NotificationDispatcher(
        outbox_path=tmp_path / "outbox.jsonl", dry_run=False,
        teams_poster=lambda url, card: (200, "Invalid webhook"),
    )
    result = dispatcher.send_teams_card("https://example.invalid/hook", "Title", "Summary")
    assert result["delivered"] is False
    assert "teams_webhook_failed" in result["error"]


def test_teams_success_is_reported_as_delivered(tmp_path):
    dispatcher = NotificationDispatcher(
        outbox_path=tmp_path / "outbox.jsonl", dry_run=False,
        teams_poster=lambda url, card: (200, "1"),
    )
    result = dispatcher.send_teams_card("https://example.invalid/hook", "T", "S")
    assert result["delivered"] is True
    assert result["status"] == 200


def test_zalo_api_error_is_surfaced(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.config.settings.ZALO_OA_ACCESS_TOKEN", "tok", raising=False)
    dispatcher = NotificationDispatcher(
        outbox_path=tmp_path / "outbox.jsonl", dry_run=False,
        zalo_sender=lambda payload: (200, {"error": 1301, "message": "template invalid"}),
    )
    result = dispatcher.send_zalo(["u1"], "tpl", {"a": 1})
    assert result["delivered"] is False
    assert result["error"] == "zalo_api_error:1301"


def test_smtp_send_path_uses_the_injected_sender(monkeypatch, tmp_path):
    monkeypatch.setattr("maia.config.settings.SMTP_HOST", "smtp.example", raising=False)
    monkeypatch.setattr("maia.config.settings.SMTP_USE_TLS", False, raising=False)
    sent = {}

    def _sender(message):
        sent["to"] = message["To"]
        sent["subject"] = message["Subject"]
        return "fake-smtp"

    dispatcher = NotificationDispatcher(
        outbox_path=tmp_path / "outbox.jsonl", dry_run=False, smtp_sender=_sender
    )
    result = dispatcher.send_email("lead@game.example", "Subject", "Body text")
    assert result["delivered"] is True
    assert sent["to"] == "lead@game.example"
    assert result["server"] == "fake-smtp"


# --------------------------------------------------------------------------- #
# SQL analytics
# --------------------------------------------------------------------------- #
def _seeded_warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "market.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE reviews (review_id TEXT PRIMARY KEY, game_id TEXT, rating INTEGER,
            body TEXT, review_date TEXT);
        CREATE TABLE marketing_metrics (metric_date TEXT, game_id TEXT, channel TEXT,
            impressions INTEGER, clicks INTEGER, installs INTEGER, spend_usd REAL,
            revenue_usd REAL, PRIMARY KEY (metric_date, game_id, channel));
        """
    )
    connection.execute("INSERT INTO reviews VALUES ('r1','g1',1,'lag','2026-09-20')")
    connection.execute(
        "INSERT INTO marketing_metrics VALUES ('2026-09-20','g1','facebook',"
        "1000,50,10,100.0,250.0)"
    )
    connection.commit()
    connection.close()
    return path


def test_sql_analytics_lists_and_describes(tmp_path):
    client = _client(build_sql_analytics_server(_seeded_warehouse(tmp_path)))
    tables = client.call_tool("list_tables", {})["structured"]
    assert {"reviews", "marketing_metrics"} <= set(tables["tables"])

    described = client.call_tool("describe_table", {"table": "reviews"})["structured"]
    assert {c["name"] for c in described["columns"]} >= {"review_id", "rating", "body"}
    assert described["row_count"] == 1


def test_sql_analytics_blocks_writes_and_unknown_tables(tmp_path):
    client = _client(build_sql_analytics_server(_seeded_warehouse(tmp_path)))
    for sql in ("DELETE FROM reviews", "DROP TABLE reviews",
                "SELECT * FROM sqlite_master", "SELECT 1; SELECT 2"):
        result = client.call_tool("run_sql", {"sql": sql})
        assert result["ok"] is False, sql
        assert result["structured"]["error"] in {
            "sql_blocked", "table_not_allowed", "query_failed"
        }
    assert client.call_tool("describe_table", {"table": "sqlite_master"})["ok"] is False


def test_sql_analytics_caps_rows_and_reports_the_guard(tmp_path):
    client = _client(build_sql_analytics_server(_seeded_warehouse(tmp_path)))
    result = client.call_tool("run_sql",
                              {"sql": "SELECT * FROM reviews", "max_rows": 1})
    assert result["ok"] is True
    assert result["structured"]["guard"]["limited"] is True
    assert result["structured"]["row_count"] == 1


def test_sql_analytics_connection_is_read_only(tmp_path):
    """Even bypassing the guard, the engine refuses a write."""
    from maia.mcp.servers.sql_analytics_server import open_readonly

    connection = open_readonly(_seeded_warehouse(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM reviews")
    finally:
        connection.close()


def test_sql_analytics_kpi_summary_is_parameterised(tmp_path):
    client = _client(build_sql_analytics_server(_seeded_warehouse(tmp_path)))
    result = client.call_tool("game_kpi_summary", {"game_id": "g1"})["structured"]
    assert result["row_count"] == 1
    row = result["rows"][0]
    assert row["channel"] == "facebook"
    assert row["avg_cpi"] == pytest.approx(10.0)
    assert row["roas"] == pytest.approx(2.5)


def test_sql_analytics_missing_warehouse_is_a_clean_tool_error(tmp_path):
    client = _client(build_sql_analytics_server(tmp_path / "absent.db"))
    result = client.call_tool("list_tables", {})
    assert result["ok"] is False
    assert result["structured"]["error"] == "warehouse_missing"


# --------------------------------------------------------------------------- #
# Market insight
# --------------------------------------------------------------------------- #
def test_market_insight_refuses_path_traversal(tmp_path):
    client = _client(build_market_insight_server(db_path=tmp_path / "m.db",
                                                 data_dir=tmp_path))
    result = client.call_tool("ingest_reviews",
                              {"path": "../../etc/passwd", "game_id": "g1"})
    assert result["ok"] is False
    assert "escapes" in result["content"][0]["text"]


def test_market_insight_ingest_then_analyse(tmp_path):
    db = tmp_path / "m.db"
    data_dir = tmp_path / "samples"
    data_dir.mkdir()
    (data_dir / "reviews.jsonl").write_text(
        '{"game_id":"g1","rating":1,"date":"2026-09-20","body":"lag khung khiep"}\n'
        '{"game_id":"g1","rating":5,"date":"2026-09-21","body":"rat vui, muot"}\n',
        encoding="utf-8",
    )
    client = _client(build_market_insight_server(db_path=db, data_dir=data_dir))

    ingested = client.call_tool("ingest_reviews",
                                {"path": "reviews.jsonl", "game_id": "g1"})
    assert ingested["structured"]["rows_new"] == 2
    assert ingested["structured"]["ok"] is True

    payload = client.call_tool("review_sentiment_summary", {"game_id": "g1"})["structured"]
    assert payload["total"] == 2
    assert payload["method"] == "lexicon_v1"  # the method is never overstated
    assert payload["sentiment_breakdown"]["positive"] == 1
    assert payload["sentiment_breakdown"]["negative"] == 1
    assert payload["caveats"]


def test_market_insight_nl_to_sql_refuses_unsupported_question(tmp_path):
    data_dir = tmp_path / "samples"
    data_dir.mkdir()
    (data_dir / "metrics.csv").write_text(
        "metric_date,game_id,channel,impressions,clicks,installs,spend_usd\n"
        "2026-09-20,g1,facebook,1000,50,10,100.0\n",
        encoding="utf-8",
    )
    client = _client(build_market_insight_server(db_path=tmp_path / "m.db",
                                                 data_dir=data_dir))
    client.call_tool("ingest_metrics", {"path": "metrics.csv"})

    supported = client.call_tool("nl_to_sql", {"question": "CPI theo kenh la bao nhieu?"})
    assert supported["structured"]["supported"] is True
    assert supported["structured"]["rows"]

    refused = client.call_tool("nl_to_sql", {"question": "So doanh thu theo quoc gia?"})
    assert refused["ok"] is False
    assert refused["structured"]["supported"] is False
    assert refused["structured"]["reason"]


def test_market_insight_retention_check_reports_insufficient_history(tmp_path):
    data_dir = tmp_path / "samples"
    data_dir.mkdir()
    (data_dir / "metrics.csv").write_text(
        "metric_date,game_id,channel,retention_d1\n2026-09-20,g1,facebook,0.4\n",
        encoding="utf-8",
    )
    client = _client(build_market_insight_server(db_path=tmp_path / "m.db",
                                                 data_dir=data_dir))
    client.call_tool("ingest_metrics", {"path": "metrics.csv"})
    result = client.call_tool("retention_drop_check", {"game_id": "g1"})["structured"]
    assert result["alert"] is False
    assert result["reason"] == "insufficient_history"


def test_build_server_dispatch_matches_the_registry_names():
    for name in ("airtable", "notification", "sql_analytics", "market_insight"):
        assert build_server(name).info.name == name


