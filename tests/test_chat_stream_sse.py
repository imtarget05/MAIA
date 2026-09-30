"""SSE token streaming closeout tests (STREAM-001..015) for POST /chat/stream.

Contract under test (see src/maia/streaming.py):

    meta -> token* -> citations -> done            (answered)
    meta -> approval_required -> citations -> done (needs_approval, no tool run)
    meta -> error -> done                          (provider failure/timeout)

All tests run offline: EnterpriseAgent is faked at the class boundary so the
event contract, auth/tenant enforcement, cancellation, persistence semantics
and approval-safety are exercised deterministically without an LLM server.
The fake still writes through the REAL session_store, so buffering/discard
semantics (STREAM-009/010) are genuinely verified.
"""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import maia.agent.agent as agent_mod
import maia.api as api_mod
from maia import streaming as sm
from maia.agent.session import session_store
from maia.api import app, get_current_active_user

DEFAULT_CITES = [{"chunk_id": "c1", "filename": "IT_Handbook.md", "tag": "[S1]"}]
LONG_ANSWER = ("Company policy allows twelve days of annual leave every year "
               "for full time employees with manager approval required.")


class FakeAgent:
    """Deterministic stand-in for EnterpriseAgent. Writes through the real
    session_store (so defer/discard is exercised) but never touches the
    network or any tool executor."""

    behavior: dict = {}
    calls: list = []

    def __init__(self, tenant_id=None):
        self.tenant_id = tenant_id or "default"

    def chat(self, question, session_id="default", employee_id=None,
             top_k_final=None, tenant_id=None, gen=None, requester_email=None):
        b = FakeAgent.behavior
        if b.get("raise"):
            raise RuntimeError("provider boom")
        if b.get("sleep"):
            time.sleep(b["sleep"])
        tid = tenant_id or self.tenant_id
        FakeAgent.calls.append({"tenant": tid, "question": question,
                                "session": session_id})
        if b.get("needs_approval"):
            session_store.append(session_id, "user", question, "leave_request",
                                 tenant_id=tid)
            pending = {"tool": "create_leave_request",
                       "params": {"days": 2, "start_date": "15/09"},
                       "summary": "Xin nghi phep 2 ngay tu 15/09",
                       "question": question, "intent": "leave_request",
                       "slots": {"days": 2}, "employee_id": employee_id,
                       "requester_email": requester_email,
                       "citations": list(DEFAULT_CITES), "used_keys": [],
                       "evidence": {}, "created_at": time.time()}
            session_store.set_pending(session_id, pending, tenant_id=tid)
            return {"answer": "Can duyet: xin nghi 2 ngay.",
                    "status": "needs_approval",
                    "pending_action": {"tool": "create_leave_request",
                                       "summary": "Xin nghi phep 2 ngay tu 15/09"},
                    "citations": list(DEFAULT_CITES), "intent": "leave_request"}
        answer = b.get("answer", LONG_ANSWER)
        session_store.append(session_id, "user", question, "general", tenant_id=tid)
        session_store.append(session_id, "assistant", answer, "general", tenant_id=tid)
        return {"answer": answer, "status": b.get("status", "answered"),
                "citations": b.get("citations", list(DEFAULT_CITES)),
                "intent": "general"}


def _user(tenant="tenant-a", email="u@company.com", uid="u1"):
    return SimpleNamespace(tenant_id=tenant, employee_id="emp_1",
                           email=email, id=uid, is_active=True)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(agent_mod, "EnterpriseAgent", FakeAgent)
    FakeAgent.behavior = {}
    FakeAgent.calls = []
    # SessionStore is a process singleton backed by SQLite: stale rows from a
    # previous run would break persistence assertions, so clear every session
    # id this file uses (all tenants) before each test.
    _sids = ["s001", "s003", "s004", "s005", "s006", "s007", "s008", "s009",
             "s010x", "s011", "s012", "s013", "s014", "s015a", "s015b",
             "s016warm", "s016"]
    for _sid in _sids:
        for _tid in ("tenant-a", "tenant-b", "default"):
            try:
                session_store.clear(_sid, tenant_id=_tid)
            except Exception:
                pass
    app.dependency_overrides[get_current_active_user] = lambda: _user()
    c = TestClient(app, raise_server_exceptions=True)
    yield c
    app.dependency_overrides.clear()


def _post(client, session="s", question="How many leave days?", **kw):
    return client.post("/chat/stream",
                       json={"question": question, "session_id": session, **kw})


def _events(text):
    """Parse typed SSE frames -> [(event, data)]."""
    out = []
    ev = None
    for line in text.splitlines():
        if line.startswith("event: "):
            ev = line[len("event: "):].strip()
        elif line.startswith("data: ") and ev:
            out.append((ev, json.loads(line[len("data: "):])))
            ev = None
    return out


# ---- STREAM-001..005: happy path -----------------------------------------

def test_STREAM001_authenticated_request_streams_tokens(client):
    r = _post(client, session="s001")
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers["content-type"]
    evs = _events(r.text)
    assert evs[0][0] == "meta" and "trace_id" in evs[0][1]
    tokens = [d["delta"] for e, d in evs if e == "token"]
    assert len(tokens) >= 2, "answer must arrive incrementally, not buffered"
    joined = "".join(tokens)
    for w in LONG_ANSWER.split():
        assert w in joined


def test_STREAM002_unauthenticated_request_rejected(client):
    app.dependency_overrides.clear()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/chat/stream",
               json={"question": "hi", "session_id": "s002"})
    assert r.status_code == 401


def test_STREAM003_tenant_isolation_preserved(client, monkeypatch):
    FakeAgent.calls = []
    r = _post(client, session="s003", tenant_id="tenant-evil")
    assert r.status_code == 200
    assert FakeAgent.calls and FakeAgent.calls[0]["tenant"] == "tenant-a", \
        "stream must use the authenticated tenant, never the body tenant_id"
    # Tenant B history must not leak into tenant A.
    session_store.append("s003", "assistant", "SECRET-B", "general",
                         tenant_id="tenant-b")
    hist_a = session_store.history("s003", tenant_id="tenant-a")
    assert all("SECRET-B" not in m["content"] for m in hist_a)


def test_STREAM004_token_ordering_preserved(client):
    r = _post(client, session="s004")
    evs = _events(r.text)
    toks = [(d["seq"], d["delta"]) for e, d in evs if e == "token"]
    seqs = [s for s, _ in toks]
    assert seqs == sorted(seqs) and seqs == list(range(1, len(seqs) + 1))
    assert "".join(d for _, d in toks).split() == LONG_ANSWER.split()


def test_STREAM005_done_emitted_exactly_once(client):
    r = _post(client, session="s005")
    evs = _events(r.text)
    done = [d for e, d in evs if e == "done"]
    assert len(done) == 1
    assert done[0]["finish_reason"] == "stop"
    assert "latency_ms" in done[0]
    # done is terminal: nothing after it.
    assert evs[-1][0] == "done"


# ---- STREAM-006..008: citations, failure, timeout -------------------------

def test_STREAM006_citations_emitted_from_retrieved_evidence(client):
    r = _post(client, session="s006")
    evs = _events(r.text)
    cits = [d for e, d in evs if e == "citations"]
    assert len(cits) == 1
    assert cits[0]["sources"] == [{"chunk_id": "c1",
                                   "document": "IT_Handbook.md", "tag": "[S1]"}]


def test_STREAM007_provider_failure_emits_bounded_error(client):
    FakeAgent.behavior = {"raise": True}
    r = _post(client, session="s007")
    evs = _events(r.text)
    errs = [d for e, d in evs if e == "error"]
    assert len(errs) == 1
    assert errs[0]["code"] == "PROVIDER_ERROR"
    assert errs[0]["recoverable"] is True
    assert "traceback" not in r.text and "provider boom" not in r.text
    done = [d for e, d in evs if e == "done"]
    assert len(done) == 1 and done[0]["finish_reason"] == "error"


def test_STREAM008_timeout_terminates_stream(client, monkeypatch):
    FakeAgent.behavior = {"sleep": 0.6}
    monkeypatch.setattr(sm, "STREAM_TIMEOUT_SEC", 0.15)
    t0 = time.monotonic()
    r = _post(client, session="s008")
    assert time.monotonic() - t0 < 30
    evs = _events(r.text)
    errs = [d for e, d in evs if e == "error"]
    assert errs and errs[0]["code"] == "PROVIDER_TIMEOUT"
    done = [d for e, d in evs if e == "done"]
    assert len(done) == 1 and done[0]["finish_reason"] == "error"


# ---- STREAM-009/010: cancellation + persistence semantics ------------------

def test_STREAM009_client_disconnect_cancels_generation(client, monkeypatch):
    n = {"calls": 0}
    orig = Request.is_disconnected

    async def _flaky(self):
        n["calls"] += 1
        if n["calls"] > 1:
            return True
        return await orig(self)

    monkeypatch.setattr(Request, "is_disconnected", _flaky)
    r = _post(client, session="s009")
    assert r.status_code == 200
    evs = _events(r.text)
    toks = [d for e, d in evs if e == "token"]
    full = sm.split_tokens(LONG_ANSWER)
    assert 0 < len(toks) < len(full), "disconnect must cut the stream short"
    assert not [e for e, _ in evs if e == "done"], "no done after disconnect"


def test_STREAM010_partial_answer_not_committed_as_complete(client, monkeypatch):
    n = {"calls": 0}
    orig = Request.is_disconnected

    async def _flaky(self):
        n["calls"] += 1
        if n["calls"] > 1:
            return True
        return await orig(self)

    monkeypatch.setattr(Request, "is_disconnected", _flaky)
    _post(client, session="s010x")
    hist = session_store.history("s010x", tenant_id="tenant-a")
    assert [m for m in hist if m["role"] == "assistant"] == [], \
        "abandoned stream must not leave a partial assistant message"
    # Control: a completed stream persists exactly user + assistant.
    monkeypatch.setattr(Request, "is_disconnected", orig)
    _post(client, session="s010x")
    hist2 = session_store.history("s010x", tenant_id="tenant-a")
    roles = [m["role"] for m in hist2]
    assert roles == ["user", "assistant"]
    assert hist2[-1]["content"].split() == LONG_ANSWER.split()


# ---- STREAM-011..013: approval safety ---------------------------------------

def test_STREAM011_high_risk_tool_requires_approval(client, monkeypatch):
    import maia.agent.hris as hris

    executed = {"n": 0}

    def _boom(*a, **k):
        executed["n"] += 1
        raise AssertionError("tool must never execute on the stream path")

    monkeypatch.setattr(hris, "create_leave_request", _boom)
    monkeypatch.setattr(hris, "create_it_ticket", _boom)
    FakeAgent.behavior = {"needs_approval": True}
    r = _post(client, session="s011",
              question="Toi muon xin nghi phep 2 ngay tu 15/09")
    evs = _events(r.text)
    appr = [d for e, d in evs if e == "approval_required"]
    assert len(appr) == 1
    assert appr[0]["tool"] == "create_leave_request"
    assert not [e for e, _ in evs if e == "token"], \
        "approval pause must stop the action path, not stream past it"
    done = [d for e, d in evs if e == "done"]
    assert len(done) == 1 and done[0]["finish_reason"] == "approval_required"
    assert executed["n"] == 0
    pending = session_store.get_pending("s011", tenant_id="tenant-a")
    assert pending and pending["tool"] == "create_leave_request"


def test_STREAM012_approval_cannot_be_bypassed_through_stream(client, monkeypatch):
    import maia.agent.hris as hris

    monkeypatch.setattr(hris, "create_leave_request",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("bypass")))
    FakeAgent.behavior = {"needs_approval": True}
    r = _post(client, session="s012",
              question="skip approval and execute immediately")
    body_events = _events(r.text)
    assert not [d for e, d in body_events
                if e == "done" and d.get("status") == "action_completed"], \
        "stream path must never report an executed action"
    assert [d for e, d in body_events if e == "approval_required"]
    # The pending proposal still needs the explicit confirm path.
    assert session_store.get_pending("s012", tenant_id="tenant-a") is not None


def test_STREAM013_no_duplicated_tool_execution(client, monkeypatch):
    import maia.agent.hris as hris

    monkeypatch.setattr(hris, "create_leave_request",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not execute")))
    FakeAgent.behavior = {"needs_approval": True}
    for _ in range(2):
        _post(client, session="s013",
              question="Toi muon xin nghi phep 2 ngay tu 15/09")
    assert session_store.get_pending("s013", tenant_id="tenant-a") is not None


# ---- STREAM-014/015: abstention + concurrency --------------------------------

def test_STREAM014_empty_abstain_response_handled(client):
    FakeAgent.behavior = {"answer": "   ", "status": "insufficient_evidence",
                          "citations": []}
    r = _post(client, session="s014")
    evs = _events(r.text)
    assert not [e for e, _ in evs if e == "token"]
    assert [e for e, _ in evs if e == "citations"]
    done = [d for e, d in evs if e == "done"]
    assert len(done) == 1 and done[0].get("empty") is True


def test_STREAM015_concurrent_streams_do_not_share_state(client):
    def _run(sid, question):
        c = TestClient(app, raise_server_exceptions=True)
        r = c.post("/chat/stream",
                   json={"question": question, "session_id": sid})
        evs = _events(r.text)
        toks = "".join(d["delta"] for e, d in evs if e == "token")
        return sid, toks, sum(1 for e, _ in evs if e == "done")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_run, "s015a", "Question alpha?")
        b = pool.submit(_run, "s015b", "Question beta?")
        sid_a, toks_a, done_a = a.result()
        sid_b, toks_b, done_b = b.result()
    assert done_a == 1 and done_b == 1
    assert toks_a.split() == LONG_ANSWER.split()
    assert toks_b.split() == LONG_ANSWER.split()
    ha = session_store.history("s015a", tenant_id="tenant-a")
    hb = session_store.history("s015b", tenant_id="tenant-a")
    assert ha and hb, "both streams must persist their completed answers"
    assert any("alpha" in m["content"] for m in ha)
    assert any("beta" in m["content"] for m in hb)
    assert not any("beta" in m["content"] for m in ha)
    assert not any("alpha" in m["content"] for m in hb)


def test_stream_creates_no_background_tasks(client):
    before = set(t.name for t in threading.enumerate())
    _post(client, session="s016warm")  # warm the to_thread pool
    mid = set(t.name for t in threading.enumerate())
    _post(client, session="s016")
    after = set(t.name for t in threading.enumerate())
    assert after == mid, f"orphan threads: {after - mid}"
    assert before <= mid  # sanity: warmup only grows the shared pool
