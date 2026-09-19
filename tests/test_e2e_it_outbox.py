"""E2E acceptance: lost-device chat → approval → IT ticket persisted → outbox drained.

Flow under test (C1 confirm-before-action):
  chat (needs_approval) → confirm (action_completed) → ticket exists in
  storage/it_tickets.json → drain_outbox() → outbox queue empty, entries moved
  to storage/outbox_history.json with dispatched flags.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.agent import EnterpriseAgent
from maia.chunking import Chunk
from maia.embeddings import Embedder
from maia.llm import CloudflareLLM
from maia.reranker import Reranker
from maia.retriever import HybridRetriever
from maia.test_utils import InMemoryVectorStore


def _agent(monkeypatch, tmp_path):
    from maia.config import settings
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "HR_MOCK_DB_PATH", str(tmp_path / "hr_mock.json"))
    monkeypatch.setattr(settings, "SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "WORKFLOW_DB_PATH", str(tmp_path / "workflow.db"))
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "TOOL_TENANT_CHECK", False)

    embedder = Embedder()
    store = InMemoryVectorStore()
    chunks = [
        Chunk(text="IT Security Policy v4.2 Section 7.1: report lost device to "
                   "IT Help Desk within 1 hour. Create a lost_device ticket via MAIA.",
              metadata={"chunk_id": "sec1", "filename": "IT_Security_Policy_v4.2.md"}),
        Chunk(text="IT Help Desk ext 202 handles lost devices and broken laptops.",
              metadata={"chunk_id": "it1", "filename": "IT_Handbook.md"}),
    ]
    for ch in chunks:
        v = embedder.embed([ch.text])[0]
        store.upsert_one(ch.metadata["chunk_id"], v,
                         {"chunk_id": ch.metadata["chunk_id"], "text": ch.text,
                          **{k: v2 for k, v2 in ch.metadata.items() if k != "chunk_id"}})
    retriever = HybridRetriever(store, embedder, storage_dir=str(tmp_path / "bm25"))
    return EnterpriseAgent(embedder, store, retriever, Reranker(), CloudflareLLM())


def test_lost_device_approval_persists_ticket_and_drains_outbox(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)

    # 1. Chat báo mất thiết bị → đề xuất ticket, CHƯA thực hiện (C1).
    r = agent.chat("Tôi làm mất laptop công ty, tạo ticket IT giúp tôi.",
                   session_id="e2e_it", employee_id="emp_e2e")
    assert r["status"] == "needs_approval"
    assert r["action"] is None
    assert r["pending_action"]["type"] == "create_it_ticket"

    # 2. Xác nhận duyệt → ticket được tạo.
    done = agent.confirm_action("e2e_it", employee_id="emp_e2e", approved=True)
    assert done["status"] == "action_completed"
    ticket_id = done["action"]["result"]["ticket_id"]

    # 3. IT ticket tồn tại trong storage/it_tickets.json.
    from maia.agent.tools import get_it_tickets
    tickets = get_it_tickets("emp_e2e")
    assert any(t["ticket_id"] == ticket_id for t in tickets)
    stored = next(t for t in tickets if t["ticket_id"] == ticket_id)
    assert stored["status"] == "OPEN"
    assert stored["tenant_id"]
    assert stored["assignee"]
    raw = json.loads((tmp_path / "it_tickets.json").read_text(encoding="utf-8"))
    assert any(t["ticket_id"] == ticket_id for t in raw)

    # 4. Outbox có mail chờ trước khi drain (proposal + decision notifications).
    from maia import notifier as nt
    assert len(nt.read_outbox()) >= 1

    # 5. Drain → hàng đợi sạch, lịch sử đã gửi có dispatched flags.
    from maia.outbox_worker import drain_outbox
    result = drain_outbox()
    assert result["dispatched"] >= 1
    assert result["pending"] == 0
    assert nt.read_outbox() == []
    history = json.loads((tmp_path / "outbox_history.json").read_text(encoding="utf-8"))
    assert len(history) >= result["dispatched"]
    assert all(h.get("dispatched") is True and h.get("dispatched_at") for h in history)


def test_admin_outbox_drain_endpoint(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from maia import notifier as nt
    from maia.api import app, get_current_active_user, get_current_admin_user
    from maia.config import settings
    from maia.models import UserRole

    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "SMTP_HOST", "")

    class _Admin:
        email = "admin@company.com"
        employee_id = "emp_admin"
        tenant_id = "default"
        is_active = True
        role = UserRole.ADMIN

    app.dependency_overrides[get_current_active_user] = lambda: _Admin()
    app.dependency_overrides[get_current_admin_user] = lambda: _Admin()
    try:
        nt.notify_new_request("create_it_ticket", "Ticket lost_device", "nv@company.com")
        assert len(nt.read_outbox()) >= 1
        client = TestClient(app, raise_server_exceptions=True)
        r = client.post("/admin/outbox/drain")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dispatched"] >= 1
        assert nt.read_outbox() == []
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_admin_user, None)
