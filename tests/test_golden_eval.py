"""Tests for MAIA-08 golden-set evaluation and MAIA-01 threshold benchmark."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import eval as eval_mod

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
SKIP_REASON = "Qdrant not available - set QDRANT_URL to run golden eval tests"


def _check_qdrant() -> bool:
    try:
        import httpx

        headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else {}
        resp = httpx.get(f"{QDRANT_URL}/healthz", headers=headers, timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def qdrant_available() -> bool:
    """Evaluate-based tests require a running Qdrant (gold sets reference real chunk ids)."""
    if not _check_qdrant():
        pytest.skip(SKIP_REASON)
    return True


def _fake_stack(tenant_id="default"):
    from maia.embeddings import Embedder
    from maia.reranker import Reranker
    from maia.retriever import HybridRetriever
    from maia.test_utils import InMemoryVectorStore

    class _FakeLLM:
        mode = "mock"

        def chat(self, messages):
            return "refuse"

    embedder = Embedder()
    store = InMemoryVectorStore()
    retriever = HybridRetriever(store, embedder, storage_dir="/tmp/ut_gold",
                                tenant_id=tenant_id)
    return embedder, store, retriever, Reranker(), _FakeLLM()


def _seed_two_tenants():
    stack = _fake_stack()
    embedder, store = stack[0], stack[1]
    for tenant in ["tA", "tB"]:
        c = __import__("maia.chunking", fromlist=["Chunk"]).Chunk(
            text=f"Chính sách nghỉ phép {tenant} 12 ngày",
            metadata={"chunk_id": f"{tenant}_c0", "filename": f"Leave_{tenant}.md"})
        v = embedder.embed([c.text])[0]
        store.upsert_one(f"{tenant}_c0", v,
                        {"chunk_id": f"{tenant}_c0", "text": c.text,
                         "filename": f"Leave_{tenant}.md", "tenant_id": tenant})
    return stack


def test_evaluate_group_metrics(qdrant_available):
    # exercise evaluate_group on a real golden file (metrics shape)
    path = eval_mod.GOLDEN_DIR / "vi_policy.jsonl"
    rep = eval_mod.evaluate_group(str(path))
    assert rep["n"] > 0
    for key in ("hit@k", "recall@k", "context_precision", "faithfulness_proxy",
                "relevance_proxy", "mrr", "false_refusal_rate"):
        assert key in rep
    assert 0 <= rep["recall@k"] <= 1


def test_unauthorized_group_has_leakage_metric(qdrant_available):
    path = eval_mod.GOLDEN_DIR / "unauthorized.jsonl"
    rep = eval_mod.evaluate_group(str(path))
    # leakage_rate is computed for groups with expect_no_evidence rows
    assert rep.get("leakage_rate") is not None


def test_no_answer_group_has_refusal_accuracy(qdrant_available):
    path = eval_mod.GOLDEN_DIR / "no_answer.jsonl"
    rep = eval_mod.evaluate_group(str(path))
    assert rep.get("refusal_accuracy") is not None


def test_benchmark_threshold_runs_and_picks(qdrant_available):
    # run a minimal benchmark on a tiny grid
    from maia import benchmark_threshold as bt
    rep = bt.benchmark_threshold(groups=["no_answer"], grid=[0.2, 0.5], top_k=3)
    assert "no_answer" in rep["groups"]
    assert rep["groups"]["no_answer"]["recommended_threshold"] in (0.2, 0.5)
    assert len(rep["groups"]["no_answer"]["per_threshold"]) == 2
