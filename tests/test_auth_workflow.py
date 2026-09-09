"""Auth identity + password reset + workflow + department notifications."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from maia import auth as auth_mod
from maia import notifier as nt
from maia import workflow as wf
from maia.config import settings
from maia.models import Base, User


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _user(db, email="nv@company.com"):
    u = User(email=email, password_hash=auth_mod.get_password_hash("secret123"),
             employee_id=auth_mod.generate_employee_id(db))
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------------------------------------------------------------- passwords + tokens
def test_password_hash_and_authenticate():
    db = _mem_db()
    u = _user(db)
    assert auth_mod.authenticate_user(db, u.email, "secret123") is not None
    assert auth_mod.authenticate_user(db, u.email, "wrong") is None
    assert auth_mod.authenticate_user(db, "ghost@company.com", "secret123") is None


def test_employee_ids_unique_incremental():
    db = _mem_db()
    a, b = _user(db, "a@company.com"), _user(db, "b@company.com")
    assert a.employee_id != b.employee_id
    assert a.employee_id.startswith("emp_")


def test_access_token_roundtrip():
    tok = auth_mod.create_access_token({"sub": "emp_001"})
    assert auth_mod.decode_token(tok)["sub"] == "emp_001"


def test_reset_token_single_use_and_expiry(monkeypatch):
    db = _mem_db()
    u = _user(db)
    raw = auth_mod.create_password_reset_token(db, u)
    assert auth_mod.consume_password_reset_token(db, raw) is not None
    assert auth_mod.consume_password_reset_token(db, raw) is None  # burned
    assert auth_mod.consume_password_reset_token(db, "bogus") is None
    # expired token
    raw2 = auth_mod.create_password_reset_token(db, u)
    monkeypatch.setattr(settings, "PASSWORD_RESET_EXPIRE_MIN", -1)
    raw3 = auth_mod.create_password_reset_token(db, u)
    assert auth_mod.consume_password_reset_token(db, raw3) is None
    assert auth_mod.consume_password_reset_token(db, raw2) is not None


# ---------------------------------------------------------------- notifier routing + outbox
def test_department_routing():
    assert nt.inbox_for("create_leave_request")[0] == "hr"
    assert nt.inbox_for("create_it_ticket")[0] == "it"
    assert nt.inbox_for("vpn")[0] == "it"
    assert nt.inbox_for("security")[0] == "security"


def test_outbox_fallback_when_no_smtp(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    r = nt.notify_new_request("create_leave_request", "Nghỉ phép 5 ngày", "nv@company.com")
    assert r["via"] == "outbox"
    box = nt.read_outbox()
    assert any("Nghỉ phép" in m["subject"] for m in box)
    assert any(m["to"] == settings.HR_EMAIL for m in box)


def test_it_ticket_goes_to_it_inbox(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    nt.notify_new_request("create_it_ticket", "Ticket LAPTOP_BROKEN", "nv@company.com")
    box = nt.read_outbox()
    assert any(m["to"] == settings.IT_EMAIL for m in box)


# ---------------------------------------------------------------- workflow store
def test_workflow_propose_decide_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WORKFLOW_DB_PATH", str(tmp_path / "wf.db"))
    p = wf.record_proposal(type="create_leave_request", session_id="s1",
                           tool="create_leave_request", requester_email="nv@company.com",
                           employee_id="emp_001", summary="Nghỉ 2 ngày",
                           params={"days": 2})
    assert p["status"] == "pending"
    # re-proposal on same session+tool updates instead of duplicating
    p2 = wf.record_proposal(type="create_leave_request", session_id="s1",
                            tool="create_leave_request", requester_email="nv@company.com",
                            employee_id="emp_001", summary="Nghỉ 3 ngày",
                            params={"days": 3})
    assert p2["id"] == p["id"]
    pendings = wf.list_requests(status="pending")
    assert len(pendings) == 1 and pendings[0]["params"]["days"] == 3
    done = wf.decide_by_session("s1", "create_leave_request", True,
                                decided_by="admin@company.com", result_ref="LV-1")
    assert done and done["status"] == "approved"
    assert wf.list_requests(status="pending") == []
    feed = wf.recent_events()
    assert {e["kind"] for e in feed} >= {"proposed", "approved"}
    assert wf.counts()["approved"] == 1


def test_workflow_reject_by_id(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WORKFLOW_DB_PATH", str(tmp_path / "wf.db"))
    p = wf.record_proposal(type="create_it_ticket", session_id="s9",
                           tool="create_it_ticket", requester_email="nv@company.com",
                           employee_id="emp_002", summary="Ticket VPN")
    out = wf.decide(p["id"], False, decided_by="admin@company.com")
    assert out["status"] == "rejected"
    assert wf.decide(p["id"], True, decided_by="admin@company.com") is None  # no double-decide
