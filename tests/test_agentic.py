import sys, tempfile, pathlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.session import session_store, SessionStore
from maia.embeddings import Embedder
from maia.stream.store import InMemoryVectorStore
from maia.retriever import HybridRetriever
from maia.reranker import Reranker
from maia.llm import CloudflareLLM
from maia.chunking import Chunk
from maia.config import settings
from maia.agent.agentic import AgenticRetriever, evidence_check
from maia.agent import hris

def _store_with_tenants():
    embedder = Embedder()
    store = InMemoryVectorStore()
    for tenant in ["tenantA","tenantB"]:
        for i in range(2):
            c = Chunk(text=f"Doc for {tenant} chunk {i} Leave Policy {tenant} Chính sách nghỉ phép", metadata={"chunk_id":f"{tenant}_c{i}", "filename":f"Leave_{tenant}.md"})
            v = embedder.embed([c.text])[0]
            store.upsert_one(c.metadata["chunk_id"], v, {"chunk_id":c.metadata["chunk_id"],"text":c.text,"filename":c.metadata["filename"],"tenant_id":tenant})
    return embedder, store

def test_memory_query_rewrite():
    session_store.clear("ut_mem")
    session_store.append("ut_mem","user","Chính sách nghỉ phép của tôi thế nào?","hr_policy")
    q = session_store.rewrite_query("ut_mem","Còn nếu tôi nghỉ 3 ngày thì sao?")
    assert "context" in q
    assert "Chính sách nghỉ phép" in q
    # non-followup should not rewrite
    q2 = session_store.rewrite_query("ut_mem","Chính sách bảo hiểm y tế là gì?")
    assert q2 == "Chính sách bảo hiểm y tế là gì?"

def test_tenant_isolation():
    embedder, store = _store_with_tenants()
    retrA = HybridRetriever(store, embedder, storage_dir="/tmp/ut_tA", tenant_id="tenantA")
    retrB = HybridRetriever(store, embedder, storage_dir="/tmp/ut_tB", tenant_id="tenantB")
    resA = retrA.retrieve("Leave Policy", tenant_id="tenantA")
    resB = retrB.retrieve("Leave Policy", tenant_id="tenantB")
    assert len(resA)==2 and all(r["metadata"]["tenant_id"]=="tenantA" for r in resA)
    assert len(resB)==2 and all(r["metadata"]["tenant_id"]=="tenantB" for r in resB)

def test_evidence_check_and_iterative():
    embedder, store = _store_with_tenants()
    retrA = HybridRetriever(store, embedder, storage_dir="/tmp/ut_iter", tenant_id="tenantA")
    reranker = Reranker()
    ar = AgenticRetriever(retrA, reranker, session_store)
    ir = ar.iterative_retrieve("Leave Policy Chính sách nghỉ phép", session_id="ut_iter1", tenant_id="tenantA")
    assert ir["attempts"]>=1
    assert ir["report"].enough is True
    ir2 = ar.iterative_retrieve("xyzabc not exist query no evidence", session_id="ut_iter2", tenant_id="tenantA")
    assert ir2["report"].enough is False
    assert ir2["attempts"]>=1

def test_hris_mock_fallback():
    tmp = tempfile.mktemp(suffix=".json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp
    settings.HRIS_ENABLED = False
    try:
        bal = hris.check_leave_balance("emp_hris_test")
        assert bal["source"]=="mock"
        assert bal["balance"]==12
        info = hris.get_employee_info("emp_hris_test")
        assert info["source"]=="mock"
        verify = hris.verify_ticket("IT-123")
        assert verify["verified"] is True
    finally:
        settings.HR_MOCK_DB_PATH = old
        pathlib.Path(tmp).unlink(missing_ok=True)

def test_agentic_tool_chain_lost_laptop():
    embedder, store = _store_with_tenants()
    # add security policy for tenantA
    c = Chunk(text="IT Security Policy v4.2 Section 7.1: Lost device report within 1 hour to IT Help Desk ext 202.", metadata={"chunk_id":"sec_c","filename":"IT_Security_Policy_v4.2.md"})
    v = embedder.embed([c.text])[0]
    store.upsert_one(c.metadata["chunk_id"], v, {"chunk_id":c.metadata["chunk_id"],"text":c.text,"filename":c.metadata["filename"],"tenant_id":"tenantA"})
    retrA = HybridRetriever(store, embedder, storage_dir="/tmp/ut_chain", tenant_id="tenantA")
    reranker = Reranker()
    llm = CloudflareLLM()
    from maia.agent.agent import EnterpriseAgent
    tmp = tempfile.mktemp(suffix=".json")
    old = settings.HR_MOCK_DB_PATH
    settings.HR_MOCK_DB_PATH = tmp
    try:
        agent = EnterpriseAgent(embedder, store, retrA, reranker, llm, tenant_id="tenantA")
        r = agent.chat("Tôi bị mất laptop, phải làm gì và tạo ticket IT giúp tôi.", session_id="ut_chain_s", employee_id="emp_chain")
        assert r["plan"]["tool"]=="create_it_ticket"
        assert r["action"] is not None
        assert r["action"]["verify"]["verified"] is True
        assert len(r["citations"])>0
        assert r["evidence"]["attempts"]>=1
    finally:
        settings.HR_MOCK_DB_PATH = old
        pathlib.Path(tmp).unlink(missing_ok=True)

def test_intent_refinement():
    from maia.agent.intents import detect_intent
    # bare policy question should be hr_policy not leave_request
    assert detect_intent("Chính sách nghỉ phép như thế nào?")=="hr_policy"
    # explicit request with 'xin' should be leave_request
    assert detect_intent("Tôi muốn xin nghỉ phép 5 ngày từ 10/09")=="leave_request"
    # followup without xin should not be leave_request
    assert detect_intent("Còn nếu tôi nghỉ 3 ngày thì sao?") in ("general","hr_policy")
