"""Reranker lifecycle: a cross-encoder must be CONSTRUCTED once per process.

Bug: pipeline_query.build_stack() did ``Reranker()`` on every call, and
Reranker.__init__ constructs a sentence_transformers.CrossEncoder (~90MB of
weights, ~30s to load on this machine). Every query() therefore re-loaded the
model before any retrieval happened.

These tests pin the lifecycle contract WITHOUT touching the network or torch:
the CrossEncoder import inside Reranker.__init__ is replaced with a fake, so
no 90MB download happens in CI either way. Retrieval-quality settings and the
default model name are deliberately untouched.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import reranker as reranker_mod

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class _FakeCrossEncoder:
    """Stand-in for sentence_transformers.CrossEncoder: records constructions,
    returns descending relevance scores so rerank() has something to order on."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        type(self).constructions.append(model_name)

    def predict(self, pairs):
        return [1.0 - 0.01 * i for i in range(len(pairs))]


@pytest.fixture
def fake_ce(monkeypatch):
    """Stub the CrossEncoder symbol Reranker imports, and count constructions."""
    _FakeCrossEncoder.constructions = []
    try:
        import sentence_transformers as st
    except Exception:  # torch/ST not installed -> fake module so the import works
        st = types.ModuleType("sentence_transformers")
        monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setattr(st, "CrossEncoder", _FakeCrossEncoder, raising=False)
    return _FakeCrossEncoder


@pytest.fixture(autouse=True)
def clean_cache():
    """Never let a warmed instance leak between tests."""
    reranker_mod.reset_reranker_cache()
    yield
    reranker_mod.reset_reranker_cache()


# --------------------------------------------------------------------------- #
# 1. the lifecycle contract: construct once, reuse twice
# --------------------------------------------------------------------------- #
def test_build_stack_constructs_reranker_once_across_three_queries(monkeypatch, fake_ce):
    """query #1 builds the model, queries #2 and #3 reuse it. Exactly one build."""
    from maia import pipeline_query as pq

    class _FakeStore:
        def __init__(self, *a, **k): pass
        def count(self): return 0

    class _FakeRetriever:
        def __init__(self, *a, **k): self.tenant_id = "t"
        def retrieve(self, question, tenant_id=None, session_id=None):
            return [{"chunk_id": "c1", "text": "Nghỉ phép 5 ngày.",
                     "metadata": {"filename": "hr.md"}, "dense_score": 0.9,
                     "fused_score": 0.9}]

    class _FakeLLM:
        mode = "mock"
        def chat(self, messages): return "Bạn được nghỉ 5 ngày."

    monkeypatch.setattr(pq, "QdrantStore", _FakeStore)
    monkeypatch.setattr(pq, "HybridRetriever", _FakeRetriever)
    monkeypatch.setattr(pq, "build_llm", lambda: _FakeLLM())

    seen = []
    for _ in range(3):  # query #1, #2, #3
        res = pq.query("xin nghỉ phép 5 ngày")
        seen.append(res["rerank_mode"])
        assert res["has_evidence"] is True

    assert fake_ce.constructions == [DEFAULT_MODEL], (
        f"expected exactly one CrossEncoder construction for 3 queries, got {fake_ce.constructions}")
    assert seen == ["cross-encoder"] * 3  # all three queries used the cross-encoder

    # and the three queries saw the SAME instance, not merely an equal-looking one
    first = reranker_mod.get_reranker()
    assert reranker_mod.get_reranker() is first
    assert pq.build_stack()[3] is first


# --------------------------------------------------------------------------- #
# 2. cache reset is deterministic
# --------------------------------------------------------------------------- #
def test_reset_reranker_cache_releases_and_forces_reconstruction(fake_ce):
    a = reranker_mod.get_reranker()
    assert fake_ce.constructions == [DEFAULT_MODEL]
    assert reranker_mod.get_reranker() is a
    assert len(fake_ce.constructions) == 1  # warm calls do not rebuild

    assert reranker_mod.reset_reranker_cache() == 1        # dropped exactly one
    assert reranker_mod.resident_reranker_mode() == "not-loaded"
    assert reranker_mod.reset_reranker_cache() == 0        # idempotent

    b = reranker_mod.get_reranker()
    assert b is not a
    assert fake_ce.constructions == [DEFAULT_MODEL] * 2

    # selective reset only drops the named model
    reranker_mod.get_reranker("other/model-x")
    assert reranker_mod.reset_reranker_cache(DEFAULT_MODEL) == 1
    assert reranker_mod.reset_reranker_cache("other/model-x") == 1
    assert reranker_mod.reset_reranker_cache() == 0


def test_resident_mode_does_not_construct_anything(fake_ce):
    """A readiness probe must not be the thing that pays the model load."""
    assert reranker_mod.resident_reranker_mode() == "not-loaded"
    assert fake_ce.constructions == []
    reranker_mod.get_reranker()
    assert reranker_mod.resident_reranker_mode() == "cross-encoder"


# --------------------------------------------------------------------------- #
# 3. the cache is keyed by model -- no cross-model collisions
# --------------------------------------------------------------------------- #
def test_different_model_name_does_not_collide_in_cache(fake_ce):
    a = reranker_mod.get_reranker("cross-encoder/ms-marco-MiniLM-L-6-v2")
    b = reranker_mod.get_reranker("BAAI/bge-reranker-base")
    c = reranker_mod.get_reranker("BAAI/bge-reranker-base")

    assert a is not b, "requesting a different model returned the old instance"
    assert c is b, "same model must hit the cache"
    assert a.model_name == DEFAULT_MODEL
    assert b.model_name == "BAAI/bge-reranker-base"
    assert fake_ce.constructions == [DEFAULT_MODEL, "BAAI/bge-reranker-base"]

    # un-keyed call is the default model, not "whatever was cached last"
    assert reranker_mod.get_reranker() is a
    assert reranker_mod.get_reranker(model=None) is a
    assert len(fake_ce.constructions) == 2


# --------------------------------------------------------------------------- #
# 4. default model unchanged (hard rule: no model promotion here)
# --------------------------------------------------------------------------- #
def test_default_model_and_ranking_config_unchanged():
    assert reranker_mod.DEFAULT_RERANK_MODEL == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    from maia.config import settings

    assert settings.SIMILARITY_THRESHOLD > 0
    assert settings.RRF_K > 0
    assert settings.TOP_K_FINAL > 0
