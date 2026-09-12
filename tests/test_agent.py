import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.intents import detect_intent, slots_for_intent
from maia.agent.session import SessionStore
from maia.agent.tools import check_leave_balance, create_it_ticket, create_leave_request


def test_intent_leave_request():
    assert detect_intent("Tôi muốn xin nghỉ phép 5 ngày") == "leave_request"

def test_intent_leave_balance():
    assert detect_intent("Số ngày phép còn lại của tôi?") == "leave_balance"

def test_intent_it_help():
    assert detect_intent("Tôi làm mất laptop công ty") in ("it_help", "security")

def test_intent_vpn():
    assert detect_intent("Cách request VPN?") == "vpn"

def test_intent_general():
    assert detect_intent("Xin chào") == "general"

def test_slots_days_and_date():
    s = slots_for_intent("Tôi muốn xin nghỉ phép 5 ngày từ 10/09", "leave_request")
    assert s["days"] == 5
    assert s["start_date"] == "10/09"

def test_slots_no_days():
    s = slots_for_intent("Tôi muốn xin nghỉ phép", "leave_request")
    assert "days" not in s

def test_leave_balance_mock():
    import tempfile

    from maia.config import settings
    # use temp file to avoid polluting real storage
    tmp = tempfile.mktemp(suffix=".json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp
    try:
        b = check_leave_balance("emp_test_001")
        assert b["balance"] == 12
        r = create_leave_request("emp_test_001", days=2, start_date="10/09")
        assert r["ok"] is True
        assert "request_id" in r
        b2 = check_leave_balance("emp_test_001")
        assert b2["balance"] == 10
        # insufficient
        r2 = create_leave_request("emp_test_001", days=20)
        assert r2["ok"] is False
    finally:
        settings.HR_MOCK_DB_PATH = old
        try: Path(tmp).unlink()
        except: pass

def test_session_store():
    s = SessionStore(max_turns=4)
    s.append("s1", "user", "xin nghỉ 5 ngày", "leave_request")
    s.append("s1", "assistant", "ok", "leave_request")
    assert len(s.history("s1")) == 2
    slots = s.get_slots("s1")
    assert slots.get("days") == 5

def test_create_it_ticket(tmp_path, monkeypatch):
    from maia.config import settings
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "HR_MOCK_DB_PATH", str(tmp_path / "hr_mock.json"))
    monkeypatch.setattr(settings, "TOOL_TENANT_CHECK", False)
    t = create_it_ticket("emp_001", "vpn_request", "need vpn")
    assert t["ok"] is True
    assert "ticket_id" in t
    assert t["type"] == "vpn_request"

def test_agent_chat_needs_clarification():
    from maia.agent.agent import EnterpriseAgent
    from maia.chunking import Chunk
    from maia.embeddings import Embedder
    from maia.llm import CloudflareLLM
    from maia.reranker import Reranker
    from maia.retriever import HybridRetriever
    from maia.test_utils import InMemoryVectorStore

    # minimal in-memory stack
    embedder = Embedder()
    store = InMemoryVectorStore()
    # inject some enterprise chunks directly
    chunks = [
        Chunk(text="Leave Policy: each employee has 12 annual leave days. Request via MAIA.", metadata={"chunk_id": "c1", "filename": "Leave_Policy.md"}),
        Chunk(text="IT Security Policy v4.2 Section 7.1: report lost device to IT Help Desk within 1 hour.", metadata={"chunk_id": "c2", "filename": "IT_Security_Policy_v4.2.md"}),
    ]
    vecs = embedder.embed([c.text for c in chunks])
    for c, v in zip(chunks, vecs):
        store.upsert_one(c.metadata["chunk_id"], v, {"chunk_id": c.metadata["chunk_id"], "text": c.text, "filename": c.metadata["filename"]})
    retriever = HybridRetriever(store, embedder, storage_dir="/tmp/maia_test_storage")
    reranker = Reranker()
    llm = CloudflareLLM()
    agent = EnterpriseAgent(embedder, store, retriever, reranker, llm)

    # missing slots -> clarification
    res = agent.chat("Tôi muốn xin nghỉ phép", session_id="test_clarify_001")
    assert res["needs_clarification"] is True
    assert res["intent"] == "leave_request"

    # full slots -> PROPOSES request (C1: no side effect without approval)
    import tempfile

    from maia.config import settings
    tmp = tempfile.mktemp(suffix=".json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp
    try:
        res2 = agent.chat("Tôi muốn xin nghỉ phép 2 ngày từ 15/09", session_id="test_full_001", employee_id="emp_agent_test")
        assert res2["intent"] == "leave_request"
        assert res2["status"] == "needs_approval"
        assert res2["action"] is None  # nothing executed yet
        assert res2["pending_action"]["type"] == "create_leave_request"
        # nothing was deducted before approval
        assert check_leave_balance("emp_agent_test")["balance"] == 12
        # approve -> executes exactly once
        res3 = agent.confirm_action("test_full_001", employee_id="emp_agent_test", approved=True)
        assert res3["status"] == "action_completed"
        assert res3["action"]["result"]["ok"] is True
        assert check_leave_balance("emp_agent_test")["balance"] == 10
    finally:
        settings.HR_MOCK_DB_PATH = old
        try: Path(tmp).unlink()
        except: pass
