"""MAIA-07: tests for 7-stage pipeline tracing + correlation_id."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.observability import PipelineTracer

# --- PipelineTracer unit tests ---

def test_tracer_generates_correlation_id():
    t = PipelineTracer()
    assert len(t.correlation_id) == 12


def test_tracer_accepts_custom_correlation_id():
    t = PipelineTracer(correlation_id="abc123")
    assert t.correlation_id == "abc123"


def test_all_7_stages_emitted_with_same_correlation_id():
    t = PipelineTracer()
    for stage in PipelineTracer.STAGES:
        t.log(stage, foo="bar")
    ids = {s.correlation_id for s in t.spans}
    assert len(ids) == 1
    assert len(t.spans) == 7


def test_unknown_stage_raises():
    t = PipelineTracer()
    with pytest.raises(ValueError, match="unknown stage"):
        t.log("nonexistent_stage")


def test_finalize_returns_full_trace():
    t = PipelineTracer()
    t.log("query_input", q="test")
    t.log("retrieval", n=5)
    trace = t.finalize()
    assert trace["correlation_id"] == t.correlation_id
    assert trace["n_stages"] == 2
    assert trace["stages"][0]["stage"] == "query_input"


def test_finalize_redacted_drops_text_fields():
    t = PipelineTracer()
    t.log("query_input", query="sensitive query")
    t.log("generation", answer="sensitive answer", llm_mode="mock")
    trace = t.finalize_redacted()
    # text-bearing fields dropped
    assert "query" not in trace["stages"]["query_input"]
    assert "answer" not in trace["stages"]["generation"]
    # structural fields kept
    assert trace["stages"]["generation"]["llm_mode"] == "mock"


# --- Integration: query() emits 7 stages ---

def test_query_emits_all_7_stages(monkeypatch):
    """A successful query should emit exactly 7 stage logs with the same correlation_id."""
    import maia.pipeline_query as pq
    from maia import observability
    from maia.retriever import HybridRetriever

    captured = []
    original_log = observability.PipelineTracer.log

    def spy_log(self, stage, **fields):
        captured.append((self.correlation_id, stage))
        return original_log(self, stage, **fields)

    monkeypatch.setattr(observability.PipelineTracer, "log", spy_log)
    # force mock mode so no external LLM call
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_API_TOKEN", "")
    # ensure retrieval returns candidates so the full 7-stage path runs
    monkeypatch.setattr(HybridRetriever, "retrieve",
                        lambda self, query, **kw: [
                            {"chunk_id": "c1", "text": "test", "metadata": {"filename": "t.md"},
                             "dense_score": 0.5, "bm25_score": 0.0, "fused_score": 0.5},
                        ])

    res = pq.query("chính sách nghỉ phép?", top_k_final=3)
    stages = [s for _, s in captured]
    assert set(stages) == set(PipelineTracer.STAGES)
    # all same correlation_id
    ids = {cid for cid, _ in captured}
    assert len(ids) == 1


def test_refusal_path_logs_refusal_reason(monkeypatch):
    """A refused query should log a refusal_reason at the evidence_gate stage."""
    import maia.pipeline_query as pq
    from maia import observability

    captured = {}
    original_log = observability.PipelineTracer.log

    def spy_log(self, stage, **fields):
        captured[stage] = fields
        return original_log(self, stage, **fields)

    monkeypatch.setattr(observability.PipelineTracer, "log", spy_log)
    # force cloudflare mode + high threshold so evidence gate refuses
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_ACCOUNT_ID", "x")
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_API_TOKEN", "y")
    monkeypatch.setattr(pq.settings, "SIMILARITY_THRESHOLD", 0.99)

    res = pq.query("chính sách nghỉ phép?", top_k_final=3)
    assert res.get("refused") is True
    assert "refusal_reason" in captured["evidence_gate"]
    assert captured["evidence_gate"]["refusal_reason"] == "below_threshold"


def test_trace_embedded_when_pipeline_trace_enabled(monkeypatch):
    import maia.pipeline_query as pq
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_API_TOKEN", "")
    monkeypatch.setattr(pq.settings, "PIPELINE_TRACE", True)
    res = pq.query("chính sách nghỉ phép?", top_k_final=3)
    assert "_trace" in res
    assert res["_trace"]["n_stages"] == 7


def test_trace_not_embedded_by_default(monkeypatch):
    import maia.pipeline_query as pq
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_API_TOKEN", "")
    monkeypatch.setattr(pq.settings, "PIPELINE_TRACE", False)
    res = pq.query("chính sách nghỉ phép?", top_k_final=3)
    assert "_trace" not in res
