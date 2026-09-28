"""Showcase-scenario tests: the three interview demos, offline and end to end.

Scenarios are the artefact a reviewer actually runs, so they are tested for the
properties that make a demo trustworthy: they use only the bundled fixtures, they
finish without credentials, and they never claim a message was delivered.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maia.scenarios import SCENARIOS, describe_scenarios, run_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "market"
SAMPLE_DIR = DATA_DIR / "samples"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point every storage path at a temp dir; the repo data dir is read-only."""
    monkeypatch.setattr("maia.config.settings.MARKET_DB_PATH",
                        str(tmp_path / "market.db"), raising=False)
    monkeypatch.setattr("maia.config.settings.MARKET_DATA_DIR", str(DATA_DIR),
                        raising=False)
    monkeypatch.setattr("maia.config.settings.NOTIFICATION_OUTBOX_PATH",
                        str(tmp_path / "outbox.jsonl"), raising=False)
    monkeypatch.setattr("maia.config.settings.MCP_AUDIT_LOG_PATH",
                        str(tmp_path / "audit.jsonl"), raising=False)
    monkeypatch.setattr("maia.config.settings.STORAGE_DIR", str(tmp_path),
                        raising=False)
    return tmp_path


def test_catalogue_lists_every_scenario():
    entries = describe_scenarios()
    assert {e["name"] for e in entries} == set(SCENARIOS)
    assert all(e["title"] and e["example_params"] for e in entries)


def test_unknown_scenario_raises_with_the_known_list():
    with pytest.raises(KeyError, match="known"):
        run_scenario("does_not_exist")


def test_review_insight_scenario_ingests_and_analyses(isolated):
    out = run_scenario("review_insight_report",
                       {"fixture": "samples/game_reviews.jsonl", "game_id": "demo-game"})
    assert out["ok"] is True
    assert [s["step"] for s in out["steps"]] == ["ingest", "query"]
    assert out["insight"]["sample_size"] == 8
    assert out["insight"]["method"] == "lexicon_v1"
    assert out["insight"]["topics"]


def test_review_insight_scenario_reports_an_unusable_fixture(isolated):
    out = run_scenario("review_insight_report",
                       {"fixture": "nope.jsonl", "game_id": "demo-game"})
    assert any("fixture unusable" in w for w in out["warnings"])
    assert out["insight"] is None


def test_campaign_content_sync_uses_a_versioned_prompt_and_dry_runs(isolated):
    out = run_scenario("campaign_content_sync",
                       {"game_name": "Game Demo", "channels": "facebook, zalo"})
    assert out["ok"] is True
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["render_prompt"]["prompt"] == "campaign_copywriter@1.1.0"
    assert steps["render_prompt"]["content_hash"]
    assert steps["render_prompt"]["parameters"]["temperature"] == 0.7
    assert steps["airtable_sync"]["created"] == 2
    assert out["dry_run"] is True  # no Airtable credentials in a test env
    assert out["variants"]


def test_campaign_content_sync_accepts_a_provided_draft(isolated):
    draft = ('{"campaign_id": "c1", "channel_variants": [{"channel": "facebook",'
             ' "headline": "Headline du lieu",'
             ' "body": "Noi dung du dieu chung cho kich ban.", "cta": "Vao game"}]}')
    out = run_scenario("campaign_content_sync", {"draft_json": draft})
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["draft_copy"]["source"] == "provided"
    assert steps["airtable_sync"]["created"] == 1


def test_kpi_alert_scenario_stays_silent_without_data(isolated):
    """No metrics ingested -> no alert, and the scenario says so."""
    out = run_scenario("kpi_alert_fanout", {"game_id": "demo-game"})
    assert out["ok"] is True
    assert out["alerts"] == []
    assert any("chưa có dữ liệu" in w or "nothing was sent" in w
               for w in out["warnings"])


def test_kpi_alert_scenario_fans_out_and_flags_dry_run(isolated):
    from maia.pipeline import FileConnector, MarketWarehouse, ingest_metrics
    from maia.pipeline.analytics import severity_for

    ingest_metrics(
        FileConnector(SAMPLE_DIR / "marketing_metrics.csv"),
        MarketWarehouse(isolated / "market.db"),
    )
    out = run_scenario("kpi_alert_fanout", {"game_id": "demo-game"})
    detection = out["detection"]
    assert detection["alert"] is True
    # The severity ladder is the same one the retention_drop_briefing prompt uses.
    assert detection["severity"] == severity_for(detection["pct_change"])
    assert out["alerts"], "the fanout must have executed at least one tool"
    assert {a["structured"]["channel"] for a in out["alerts"]} == {"email", "teams"}
    assert out["dry_run"] is True  # honest: nothing was really delivered


def test_scenarios_are_json_serialisable(isolated):
    for name in SCENARIOS:
        out = run_scenario(name, {"fixture": "samples/game_reviews.jsonl",
                                  "game_id": "demo-game"})
        assert json.loads(json.dumps(out))  # API/UI contract: plain JSON only