"""P0/C1 tests: evidence gate, confirm-before-action, ticket taxonomy.

Contract under test:
- insufficient evidence -> status=insufficient_evidence, NO LLM call, NO tool call
- side-effect tools -> status=needs_approval (propose only), execute on confirm
- lost device -> lost_device ticket, never laptop_broken
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.agent import INSUFFICIENT_TEXT, EnterpriseAgent
from maia.agent.agentic import evidence_check
from maia.agent.intents import detect_intent, ticket_type_for
from maia.chunking import Chunk
from maia.embeddings import Embedder
from maia.llm import CloudflareLLM
from maia.reranker import Reranker
from maia.retriever import HybridRetriever
from maia.test_utils import InMemoryVectorStore


def _agent(chunks, llm=None, tenant_id=None):
    embedder = Embedder()
    store = InMemoryVectorStore()
    for ch in chunks:
        v = embedder.embed([ch.text])[0]
        store.upsert_one(ch.metadata["chunk_id"], v,
                         {"chunk_id": ch.metadata["chunk_id"], "text": ch.text,
                          **{k: v2 for k, v2 in ch.metadata.items() if k != "chunk_id"}})
    retriever = HybridRetriever(store, embedder, storage_dir="/tmp/maia_test_approval")
    agent = EnterpriseAgent(embedder, store, retriever, Reranker(), llm or CloudflareLLM(),
                            tenant_id=tenant_id)
    return agent


def _leave_chunks():
    return [
        Chunk(text="Leave Policy: each employee has 12 annual leave days. Request via MAIA with approval.",
              metadata={"chunk_id": "lp1", "filename": "Leave_Policy.md"}),
        Chunk(text="IT Security Policy v4.2 Section 7.1: report lost device to IT Help Desk within 1 hour.",
              metadata={"chunk_id": "sec1", "filename": "IT_Security_Policy_v4.2.md"}),
    ]


class ExplodingLLM:
    mode = "cloudflare"  # force strict grounding path
    def chat(self, messages):
        raise AssertionError("LLM must NOT be called without sufficient evidence")


def test_insufficient_evidence_no_llm_no_tool():
    agent = _agent(_leave_chunks(), llm=ExplodingLLM())
    r = agent.chat("xyzabc quantum entanglement provision government tender",
                   session_id="ap_insuf_001")
    assert r["status"] == "insufficient_evidence"
    assert r["answer"] == INSUFFICIENT_TEXT
    assert r["has_evidence"] is False
    assert r["action"] is None
    assert r["pending_action"] is None
    assert r["grounding"]["supported"] is False


def test_insufficient_keeps_retrieved_trace():
    agent = _agent(_leave_chunks())
    r = agent.chat("xyzabc quantum entanglement provision government tender",
                   session_id="ap_insuf_002")
    assert r["status"] == "insufficient_evidence"
    assert isinstance(r["retrieved"], list)
    assert r["evidence"]["reason"] in ("dense_low", "topical_low", "no_vectors")


def test_ticket_proposed_not_executed():
    agent = _agent(_leave_chunks())
    r = agent.chat("Tôi làm mất laptop công ty. Tôi cần làm gì?",
                   session_id="ap_prop_001", employee_id="emp_ap_1")
    assert r["status"] == "needs_approval"
    assert r["action"] is None  # C1: nothing executed
    assert r["pending_action"]["type"] == "create_it_ticket"
    assert r["pending_action"]["params"]["ticket_type"] == "lost_device"
    assert len(r["citations"]) > 0  # grounded proposal


def test_cancel_action_no_side_effect():
    agent = _agent(_leave_chunks())
    agent.chat("Tôi làm mất laptop công ty. Tôi cần làm gì?",
               session_id="ap_cancel_001", employee_id="emp_ap_2")
    r = agent.confirm_action("ap_cancel_001", employee_id="emp_ap_2", approved=False)
    assert r["status"] == "action_cancelled"
    assert r["action"]["status"] == "cancelled"
    # pending consumed: second confirm finds nothing
    r2 = agent.confirm_action("ap_cancel_001", employee_id="emp_ap_2", approved=True)
    assert r2["status"] == "error"


def test_confirm_without_pending_errors():
    agent = _agent(_leave_chunks())
    r = agent.confirm_action("ap_nopending_001", approved=True)
    assert r["status"] == "error"


def test_taxonomy_lost_vs_broken():
    assert detect_intent("Tôi làm mất laptop công ty") == "security"
    assert ticket_type_for("Tôi làm mất laptop công ty", "security") == "lost_device"
    assert ticket_type_for("Laptop bị hỏng không khởi động được", "it_help") == "laptop_broken"
    assert ticket_type_for("Cách request VPN?", "vpn") == "vpn_request"


def test_howto_question_answers_without_proposal():
    agent = _agent(_leave_chunks() + [
        Chunk(text="IT Help Desk ext 202 handles broken laptops. Bring the device for repair.",
              metadata={"chunk_id": "it1", "filename": "IT_Handbook.md"}),
    ])
    r = agent.chat("Laptop bị hỏng thì liên hệ bộ phận nào?",
                   session_id="ap_howto_001")
    assert r["status"] == "answered"
    assert r["pending_action"] is None
    assert r["action"] is None


def test_evidence_topical_low():
    used = [{"chunk_id": "c1", "text": "Quantum field theory notes", "dense_score": 0.9,
             "bm25_score": 0.0, "metadata": {}}]
    rep = evidence_check(used, "Quantum field theory notes",
                         query="Chính sách nghỉ phép như thế nào?")
    assert rep.enough is False
    assert rep.reason == "topical_low"
    # same dense score but topically supporting context passes
    used2 = [{"chunk_id": "c2", "text": "Leave Policy annual leave days",
              "dense_score": 0.9, "bm25_score": 0.0, "metadata": {}}]
    rep2 = evidence_check(used2, "Leave Policy annual leave days",
                          query="Chính sách nghỉ phép như thế nào?")
    assert rep2.enough is True
    assert rep2.reason == "ok"


def test_typed_contract_shape():
    agent = _agent(_leave_chunks())
    r = agent.chat("Chính sách nghỉ phép như thế nào?", session_id="ap_shape_001")
    for key in ("status", "answer", "intent", "citations", "retrieved",
                "evidence", "grounding", "action", "pending_action", "slots"):
        assert key in r, f"missing typed key: {key}"
    assert r["status"] in ("answered", "insufficient_evidence")
    if r["status"] == "answered":
        assert r["grounding"]["supported"] is True
        for c in r["citations"]:
            assert c["tag"].startswith("[S")
            assert c["relevance"] in ("high", "medium", "low")
            assert c["text"]  # first-class evidence excerpt
