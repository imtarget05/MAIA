"""Tests for Corrective RAG (CRAG): grading, refinement, fallback, wiring.

CRAG is opt-in (CRAG_ENABLED=false by default): these tests exercise the
module directly plus the disabled-by-default fallback path, fully offline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.config import settings
from maia.loops.corrective_rag import (
    CorrectiveRetriever,
    GradeLabel,
    KnowledgeRefiner,
    RetrievalGrader,
    WebSearchFallback,
)


class MockRetriever:
    def retrieve(self, query: str, tenant_id: str | None = None):
        return [
            {"chunk_id": "c1", "text": "Leave policy: 12 days annual leave per year.",
             "dense_score": 0.8, "metadata": {}},
            {"chunk_id": "c2", "text": "IT security: lost device must be reported within 24h.",
             "dense_score": 0.7, "metadata": {}},
            {"chunk_id": "c3", "text": "Benefits: gym membership up to $50/month.",
             "dense_score": 0.6, "metadata": {}},
        ]


class MockReranker:
    def rerank(self, query: str, candidates: list[dict], top_k: int = 3):
        for c in candidates:
            c["rerank_score"] = c.get("dense_score", 0.0)
        return candidates[:top_k]


class MockLLM:
    mode = "mock"

    def chat(self, messages, max_tokens=10, temperature=0.0):
        return "CORRECT"


def _assemble(cands):
    parts, used = [], []
    for i, c in enumerate(cands):
        parts.append(f"[S{i + 1}] {c['text']}")
        used.append({**c, "cite_tag": f"[S{i + 1}]"})
    return "\n".join(parts), used


def test_grader_heuristic_correct():
    grader = RetrievalGrader(use_llm=False)
    chunks = [{"chunk_id": "c1", "text": "Leave policy annual leave",
               "dense_score": 0.8, "bm25_score": 1.0}]
    results = grader.grade("nghi phép năm", chunks)
    assert results[0].label == GradeLabel.CORRECT


def test_grader_heuristic_incorrect():
    grader = RetrievalGrader(use_llm=False)
    chunks = [{"chunk_id": "c1", "text": "Unrelated content about cooking",
               "dense_score": 0.1, "bm25_score": 0.0}]
    results = grader.grade("nghi phép năm", chunks)
    assert results[0].label == GradeLabel.INCORRECT


def test_grader_heuristic_ambiguous():
    grader = RetrievalGrader(use_llm=False)
    chunks = [{"chunk_id": "c1", "text": "Policy mentions leave briefly",
               "dense_score": 0.2, "bm25_score": 0.0}]
    results = grader.grade("nghi phép năm", chunks)
    assert results[0].label == GradeLabel.AMBIGUOUS


def test_grader_llm_path():
    grader = RetrievalGrader(llm=MockLLM(), use_llm=True)
    chunks = [{"chunk_id": "c1", "text": "Leave policy annual leave", "dense_score": 0.5}]
    results = grader.grade("nghi phép", chunks)
    assert results[0].label == GradeLabel.CORRECT


def test_refiner_strips_irrelevant():
    refiner = KnowledgeRefiner()
    chunks = [{"chunk_id": "c1",
               "text": "Leave policy is 12 days. The cafeteria serves pizza on Fridays."}]
    refined = refiner.refine("nghi phép", chunks)
    assert "pizza" not in refined[0]["text"]
    assert "12 days" in refined[0]["text"]


def test_web_fallback_disabled_by_default():
    assert settings.CRAG_WEB_SEARCH_ENABLED is False
    assert settings.CRAG_ENABLED is False
    assert WebSearchFallback().search("test query") == []


def test_corrective_retriever_correct_path():
    crag = CorrectiveRetriever(MockRetriever(), MockReranker(), _assemble)
    result = crag.retrieve_corrective("nghi phép năm", top_k_final=3)
    assert result.action == "correct"
    assert len(result.used) > 0
    assert result.attempts >= 1
    assert any(g.label == GradeLabel.CORRECT for g in result.grades)


def test_corrective_retriever_exhausted_without_web():
    crag = CorrectiveRetriever(MockRetriever(), MockReranker(), _assemble)
    result = crag.retrieve_corrective("completely unknown query xyz", top_k_final=3)
    assert result.action in ("exhausted", "correct", "refined")
    assert not any(str(c.get("chunk_id", "")).startswith("web_") for c in result.used)


def test_crag_metrics_recorded():
    from maia.stream.metrics import registry
    before = registry.get("maia_crag_retrievals_total")
    crag = CorrectiveRetriever(MockRetriever(), MockReranker(), _assemble)
    crag.retrieve_corrective("nghi phép năm", top_k_final=3)
    assert registry.get("maia_crag_retrievals_total") == before + 1


def test_agentic_corrective_falls_back_when_disabled():
    from maia.agent.agentic import AgenticRetriever
    from maia.agent.session import SessionStore
    from maia.chunking import Chunk
    from maia.embeddings import Embedder
    from maia.reranker import Reranker
    from maia.retriever import HybridRetriever
    from maia.stream.store import InMemoryVectorStore

    embedder = Embedder()
    store = InMemoryVectorStore()
    c = Chunk(text="Leave Policy: 12 annual leave days. Chính sách nghỉ phép hàng năm.",
              metadata={"chunk_id": "lc1", "filename": "Leave_Policy.md"})
    v = embedder.embed([c.text])[0]
    store.upsert_one(c.metadata["chunk_id"], v,
                     {"chunk_id": c.metadata["chunk_id"], "text": c.text,
                      "filename": "Leave_Policy.md"})
    retr = HybridRetriever(store, embedder, storage_dir="/tmp/maia_test_crag_fb")
    ar = AgenticRetriever(retr, Reranker(), SessionStore())
    out = ar.corrective_retrieve("Chính sách nghỉ phép như thế nào?",
                                 session_id="crag_fb_1")
    # disabled -> classic path: no crag keys, classic evidence reason
    assert "crag_action" not in out
    assert out["report"].enough is True


def test_agentic_corrective_enabled_path():
    from maia.agent.agentic import AgenticRetriever
    from maia.agent.session import SessionStore
    from maia.chunking import Chunk
    from maia.embeddings import Embedder
    from maia.reranker import Reranker
    from maia.retriever import HybridRetriever
    from maia.stream.store import InMemoryVectorStore

    old = settings.CRAG_ENABLED
    settings.CRAG_ENABLED = True
    try:
        embedder = Embedder()
        store = InMemoryVectorStore()
        c = Chunk(text="Leave Policy: 12 annual leave days. Chính sách nghỉ phép hàng năm.",
                  metadata={"chunk_id": "lc2", "filename": "Leave_Policy.md"})
        v = embedder.embed([c.text])[0]
        store.upsert_one(c.metadata["chunk_id"], v,
                         {"chunk_id": c.metadata["chunk_id"], "text": c.text,
                          "filename": "Leave_Policy.md"})
        retr = HybridRetriever(store, embedder, storage_dir="/tmp/maia_test_crag_on")
        ar = AgenticRetriever(retr, Reranker(), SessionStore())
        out = ar.corrective_retrieve("Chính sách nghỉ phép như thế nào?",
                                     session_id="crag_on_1")
        assert out["crag_action"] in ("correct", "refined", "web_fallback", "exhausted")
        assert isinstance(out["crag_grades"], list) and out["crag_grades"]
        assert isinstance(out["crag_trace"], list) and out["crag_trace"]
    finally:
        settings.CRAG_ENABLED = old
