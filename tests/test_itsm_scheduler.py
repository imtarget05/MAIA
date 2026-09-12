"""Scheduler + ITSM provider tests (no network, no threads by default).

- scheduler_tick dispatches queued entries (simulated send) and empties
  the queue; start_scheduler is a NO-OP unless OUTBOX_WORKER_ENABLED.
- ITSM create_it_ticket falls back to local JSON audit when disabled;
  routes to the Jira provider (mocked HTTP) when enabled.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _isolate(monkeypatch, tmp_path):
    from maia.config import settings
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "HR_MOCK_DB_PATH", str(tmp_path / "hr_mock.json"))
    monkeypatch.setattr(settings, "TOOL_TENANT_CHECK", False)
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "ITSM_ENABLED", False)
    monkeypatch.setattr(settings, "ITSM_BASE_URL", "")
    monkeypatch.setattr(settings, "OUTBOX_WORKER_ENABLED", False)
    return settings


def test_scheduler_tick_dispatches_and_cleans_queue(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maia import notifier as nt
    from maia.outbox_scheduler import scheduler_tick
    nt.notify_new_request("create_it_ticket", "Ticket lost_device", "nv@company.com")
    assert len(nt.read_outbox()) >= 1
    result = scheduler_tick()
    assert result["dispatched"] >= 1
    assert result["pending"] == 0
    assert result["dead_lettered"] == 0
    assert nt.read_outbox() == []


def test_scheduler_tick_empty_queue(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maia.outbox_scheduler import scheduler_tick
    assert scheduler_tick() == {"dispatched": 0, "pending": 0, "dead_lettered": 0}


def test_scheduler_noop_unless_enabled(monkeypatch, tmp_path):
    settings = _isolate(monkeypatch, tmp_path)
    from maia.outbox_scheduler import scheduler_status, start_scheduler
    assert start_scheduler() is False
    assert scheduler_status()["running"] is False
    settings.OUTBOX_WORKER_ENABLED = True
    settings.OUTBOX_WORKER_INTERVAL_SEC = 3600.0
    try:
        assert start_scheduler() is True
        assert scheduler_status()["running"] is True
    finally:
        from maia.outbox_scheduler import stop_scheduler
        stop_scheduler()
        settings.OUTBOX_WORKER_ENABLED = False
    assert scheduler_status()["running"] is False


def test_scheduler_deadletters_after_max_retries(monkeypatch, tmp_path):
    settings = _isolate(monkeypatch, tmp_path)
    settings.SMTP_HOST = "127.0.0.1"  # force real SMTP path -> refused
    settings.SMTP_PORT = 9  # discard port: connection refused fast
    settings.SMTP_USE_TLS = False
    settings.OUTBOX_MAX_RETRIES = 2
    from maia import notifier as nt
    from maia.outbox_scheduler import read_deadletter, scheduler_tick
    nt.notify_new_request("create_it_ticket", "Ticket x", "nv@company.com")
    first = scheduler_tick()
    assert first["pending"] == 1 and first["dead_lettered"] == 0
    second = scheduler_tick()
    assert second["pending"] == 0 and second["dead_lettered"] == 1
    assert len(read_deadletter()) == 1
    assert nt.read_outbox() == []


def test_itsm_fallback_to_local_when_disabled(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maia.agent import itsm
    from maia.agent.tools import get_it_tickets
    assert itsm.get_provider("servicenow").name == "servicenow"
    assert itsm.get_provider("unknown-name").name == "jira"
    r = itsm.create_it_ticket("emp_itsm_1", "lost_device", "lost laptop")
    assert r["ok"] is True and r["source"] == "local"
    assert r["itsm_provider"] == "jira"
    assert any(t["ticket_id"] == r["ticket_id"]
               for t in get_it_tickets("emp_itsm_1"))
    raw = json.loads((tmp_path / "it_tickets.json").read_text(encoding="utf-8"))
    assert any(t["ticket_id"] == r["ticket_id"] for t in raw)


def test_itsm_jira_provider_success_mocked(monkeypatch, tmp_path):
    settings = _isolate(monkeypatch, tmp_path)
    settings.ITSM_ENABLED = True
    settings.ITSM_BASE_URL = "https://jira.example.com"
    settings.ITSM_API_TOKEN = "tok"
    settings.ITSM_PROJECT_KEY = "IT"

    class _Resp:
        def raise_for_status(self): ...
        def json(self): return {"key": "IT-123"}

    seen: dict = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["payload"] = json
        return _Resp()

    monkeypatch.setattr("maia.agent.itsm.requests.post", _fake_post)
    from maia.agent import itsm
    r = itsm.create_it_ticket("emp_itsm_2", "lost_device", "lost laptop")
    assert r["ticket_id"] == "IT-123"
    assert r["source"] == "itsm:jira"
    assert seen["url"].endswith("/rest/api/3/issue")
    assert seen["payload"]["fields"]["project"] == {"key": "IT"}


def test_itsm_jira_failure_falls_back(monkeypatch, tmp_path):
    settings = _isolate(monkeypatch, tmp_path)
    settings.ITSM_ENABLED = True
    settings.ITSM_BASE_URL = "https://jira.example.com"

    def _boom(*a, **k):
        raise ConnectionError("no network")

    monkeypatch.setattr("maia.agent.itsm.requests.post", _boom)
    from maia.agent import itsm
    r = itsm.create_it_ticket("emp_itsm_3", "vpn_request", "need vpn")
    assert r["ok"] is True and r["source"] == "local"


def test_hris_routes_it_tickets_through_itsm(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maia.agent import hris
    r = hris.create_it_ticket("emp_hris_it", "general", "printer broken")
    assert r["ok"] is True and r["source"] == "local"
