"""P1-1/P1-8: authorization boundary — request-model tenant stripping, session
store tenant isolation, workflow state-machine enforcement.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from maia import api as api_mod
from maia import workflow as wf
from maia.agent.session import SessionStore
from maia.config import settings

# ---- request models: caller can no longer set tenant_id ------------------

NO_TENANT_MODELS = ["UrlIngestReq", "MemoryStoreReq"]  # TeamRunReq/TeamChatReq archived (_archive/)


@pytest.mark.parametrize("name", NO_TENANT_MODELS)
def test_request_models_do_not_accept_tenant_id(name):
    model = getattr(api_mod, name)
    assert "tenant_id" not in model.model_fields, f"{name} still accepts tenant_id"


@pytest.mark.parametrize("name", NO_TENANT_MODELS)
def test_request_models_ignore_extra_tenant_id(name):
    """Backward-compat: old clients sending tenant_id must not 422 (extra='ignore')."""
    model = getattr(api_mod, name)
    if name == "UrlIngestReq":
        m = model(url="https://x.com/a.pdf", session_id="s1", tenant_id="attacker")
    elif name == "MemoryStoreReq":
        m = model(user_id="u1", tenant_id="attacker")
    else:
        m = model(question="q", tenant_id="attacker")
    assert "tenant_id" not in m.model_fields


def test_query_and_chat_req_keep_tenant_for_backward_compat():
    assert "tenant_id" in api_mod.QueryReq.model_fields
    assert "tenant_id" in api_mod.ChatReq.model_fields


# ---- session store tenant isolation ---------------------------------------

def test_session_store_tenant_isolation():
    ss = SessionStore()
    ss.clear("iso_s", tenant_id="tenantA")
    ss.clear("iso_s", tenant_id="tenantB")
    ss.append("iso_s", "user", "question A", "general", tenant_id="tenantA")
    ss.append("iso_s", "user", "question B", "general", tenant_id="tenantB")
    hist_a = ss.history("iso_s", tenant_id="tenantA")
    hist_b = ss.history("iso_s", tenant_id="tenantB")
    assert len(hist_a) == 1 and hist_a[0]["content"] == "question A"
    assert len(hist_b) == 1 and hist_b[0]["content"] == "question B"


def test_session_store_pending_tenant_isolation():
    ss = SessionStore()
    ss.clear("pend_s", tenant_id="tenantA")
    ss.clear("pend_s", tenant_id="tenantB")
    ss.set_pending("pend_s", {"tool": "create_it_ticket", "tenant_id": "tenantA"},
                  tenant_id="tenantA")
    assert ss.get_pending("pend_s", tenant_id="tenantB") is None
    assert ss.get_pending("pend_s", tenant_id="tenantA") is not None


# ---- workflow state machine (P1-8) ---------------------------------------

@pytest.fixture
def wf_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WORKFLOW_DB_PATH", str(tmp_path / "wf_auth.db"))
    yield wf


def test_transition_request_rejects_illegal_move(wf_env):
    p = wf_env.record_proposal(type="create_leave_request", session_id="s1",
                               tool="create_leave_request", requester_email="nv@company.com",
                               employee_id="emp_001", summary="2 days",
                               params={"days": 2})
    rid = p["id"]
    assert wf_env.is_valid_transition("pending", "cancelled") is True
    assert wf_env.is_valid_transition("pending", "approved") is True
    assert wf_env.is_valid_transition("approved", "cancelled") is False
    wf_env.decide(rid, True, decided_by="admin@company.com")
    assert wf_env.transition_request(rid, "cancelled", decided_by="admin@company.com") is None
    assert wf_env.transition_request(rid, "approved", decided_by="admin@company.com") is None


def test_transition_request_cancel_path(wf_env):
    p = wf_env.record_proposal(type="create_it_ticket", session_id="s2",
                               tool="create_it_ticket", requester_email="nv@company.com",
                               employee_id="emp_002", summary="VPN",
                               params={"ticket_type": "vpn_request"})
    rid = p["id"]
    out = wf_env.transition_request(rid, "cancelled", decided_by="nv@company.com")
    assert out is not None
    assert out["status"] == "cancelled"
    assert wf_env.get_request(rid)["status"] == "cancelled"


def test_transition_request_tenant_boundary(wf_env):
    p = wf_env.record_proposal(type="create_leave_request", session_id="s3",
                               tool="create_leave_request", requester_email="nv@company.com",
                               employee_id="emp_001", summary="2 days",
                               params={"days": 2}, tenant_id="tenantA")
    rid = p["id"]
    assert wf_env.transition_request(rid, "approved",
                                     decided_by="x@company.com",
                                     tenant_id="tenantB") is None
    out = wf_env.transition_request(rid, "approved",
                                    decided_by="x@company.com", tenant_id="tenantA")
    assert out and out["status"] == "approved"


def test_list_requests_tenant_filter(wf_env):
    wf_env.record_proposal(type="create_leave_request", session_id="sA",
                           tool="create_leave_request", requester_email="a@company.com",
                           employee_id="emp_a", summary="a", params={}, tenant_id="tenantA")
    wf_env.record_proposal(type="create_it_ticket", session_id="sB",
                           tool="create_it_ticket", requester_email="b@company.com",
                           employee_id="emp_b", summary="b", params={}, tenant_id="tenantB")
    a = wf_env.list_requests(tenant_id="tenantA")
    b = wf_env.list_requests(tenant_id="tenantB")
    assert len(a) == 1 and a[0]["tenant_id"] == "tenantA"
    assert len(b) == 1 and b[0]["tenant_id"] == "tenantB"


# ---- confirm_action employee ownership (P1-5) -----------------------------

def _agent(chunks, llm=None, tenant_id=None):
    from maia.agent.agent import EnterpriseAgent
    from maia.embeddings import Embedder
    from maia.llm import CloudflareLLM
    from maia.reranker import Reranker
    from maia.retriever import HybridRetriever
    from maia.stream.store import InMemoryVectorStore
    embedder = Embedder()
    store = InMemoryVectorStore()
    for ch in chunks:
        v = embedder.embed([ch.text])[0]
        store.upsert_one(ch.metadata["chunk_id"], v,
                         {"chunk_id": ch.metadata["chunk_id"], "text": ch.text,
                          **{k: v2 for k, v2 in ch.metadata.items() if k != "chunk_id"}})
    retriever = HybridRetriever(store, embedder, storage_dir="/tmp/maia_auth_test")
    return EnterpriseAgent(embedder, store, retriever, Reranker(),
                           llm or CloudflareLLM(), tenant_id=tenant_id)


_LEAVE_CHUNKS = [
    type("C", (), {"text": "Leave Policy: each employee has 12 annual leave days. Request via MAIA with approval.",
                   "metadata": {"chunk_id": "lp1", "filename": "Leave_Policy.md"}})(),
    type("C", (), {"text": "IT Security Policy v4.2 Section 7.1: report lost device to IT Help Desk within 1 hour.",
                   "metadata": {"chunk_id": "sec1", "filename": "IT_Security_Policy_v4.2.md"}})(),
]


def test_confirm_action_cross_employee_blocked():
    import tempfile
    tmp = tempfile.mktemp(suffix=".json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp
    try:
        agent = _agent(_LEAVE_CHUNKS)
        # propose as empA
        agent.chat("Tôi muốn xin nghỉ phép 2 ngày từ 15/09",
                   session_id="auth_xemp", employee_id="empA")
        # confirm as empB (non-admin) → blocked
        r = agent.confirm_action("auth_xemp", employee_id="empB", approved=True)
        assert r["status"] == "error"
        assert "cross_employee_confirm_blocked" in r["flags"]
    finally:
        settings.HR_MOCK_DB_PATH = old
        try:
            Path(tmp).unlink()
        except OSError:
            pass


def test_confirm_action_admin_bypasses_employee_check():
    import tempfile
    tmp = tempfile.mktemp(suffix=".json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp
    try:
        agent = _agent(_LEAVE_CHUNKS)
        agent.chat("Tôi muốn xin nghỉ phép 2 ngày từ 15/09",
                   session_id="auth_admin", employee_id="empA")
        r = agent.confirm_action("auth_admin", employee_id="empB", approved=True, is_admin=True)
        assert r["status"] == "action_completed"
    finally:
        settings.HR_MOCK_DB_PATH = old
        try:
            Path(tmp).unlink()
        except OSError:
            pass
