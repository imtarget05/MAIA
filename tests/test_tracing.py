"""MAIA-07: tests for 7-stage pipeline tracing + correlation_id."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.observability import PipelineTracer

_QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
_QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
_SKIP_REASON = "Qdrant not available - set QDRANT_URL to run query() integration tests"


def _check_qdrant() -> bool:
    try:
        import httpx

        headers = {"api-key": _QDRANT_API_KEY} if _QDRANT_API_KEY else {}
        resp = httpx.get(f"{_QDRANT_URL}/healthz", headers=headers, timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def qdrant_available() -> bool:
    if not _check_qdrant():
        pytest.skip(_SKIP_REASON)
    return True


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

def test_query_emits_all_7_stages(qdrant_available, monkeypatch):
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

    pq.query("chính sách nghỉ phép?", top_k_final=3)
    stages = [s for _, s in captured]
    assert set(stages) == set(PipelineTracer.STAGES)
    ids = {cid for cid, _ in captured}
    assert len(ids) == 1


def test_refusal_path_logs_refusal_reason(qdrant_available, monkeypatch):
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


def test_trace_embedded_when_pipeline_trace_enabled(qdrant_available, monkeypatch):
    import maia.pipeline_query as pq
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_API_TOKEN", "")
    monkeypatch.setattr(pq.settings, "PIPELINE_TRACE", True)
    res = pq.query("chính sách nghỉ phép?", top_k_final=3)
    assert "_trace" in res
    assert res["_trace"]["n_stages"] == 7


def test_trace_not_embedded_by_default(qdrant_available, monkeypatch):
    import maia.pipeline_query as pq
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(pq.settings, "CLOUDFLARE_API_TOKEN", "")
    monkeypatch.setattr(pq.settings, "PIPELINE_TRACE", False)
    res = pq.query("chính sách nghỉ phép?", top_k_final=3)
    assert "_trace" not in res


# --- OTEL tracing (maia.tracing): opt-in OTLP exporter ---

def _otel_settings_on(monkeypatch, endpoint="http://localhost:4318/v1/traces"):
    from maia.config import settings

    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setattr(settings, "OTEL_SERVICE_NAME", "maia-test")


def _otel_settings_off(monkeypatch):
    from maia.config import settings

    monkeypatch.setattr(settings, "OTEL_ENABLED", False)


def _inmemory_provider():
    otel_sdk = pytest.importorskip("opentelemetry.sdk")  # noqa: F841
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_otel_noop_when_disabled(monkeypatch):
    """Default config: NoOp tracer/spans, init returns False, never raises."""
    from maia import tracing

    _otel_settings_off(monkeypatch)
    assert tracing.is_enabled() is False
    assert tracing.init_tracing() is False
    tracer = tracing.get_tracer()
    assert isinstance(tracer, tracing.NoOpTracer)
    with tracing.span_context("maia.retrieval", {"n_candidates": 3}) as span:
        span.set_attribute("anything", "goes")
        span.add_event("ev")
        span.end()
    assert isinstance(span, tracing.NoOpSpan)
    assert isinstance(tracing.start_span("maia.query"), tracing.NoOpSpan)


def test_otel_noop_when_no_endpoint(monkeypatch):
    from maia import tracing
    from maia.config import settings

    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    assert tracing.is_enabled() is False
    assert tracing.init_tracing() is False
    assert isinstance(tracing.get_tracer(), tracing.NoOpTracer)


def test_otel_noop_when_packages_missing(monkeypatch):
    """Simulated absent opentelemetry install degrades to NoOp, not ImportError."""
    import sys

    from maia import tracing

    _otel_settings_on(monkeypatch)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
    assert isinstance(tracing.get_tracer(), tracing.NoOpTracer)
    with tracing.span_context("maia.query", {"query": "secret"}) as span:
        assert isinstance(span, tracing.NoOpSpan)


def test_otel_spans_created_with_inmemory_exporter(monkeypatch):
    """Enabled OTEL nests stage spans under the request span with attributes."""
    from maia import tracing

    _otel_settings_on(monkeypatch)
    provider, exporter = _inmemory_provider()
    monkeypatch.setattr(
        tracing, "get_tracer", lambda name="maia.pipeline": provider.get_tracer(name)
    )

    with tracing.span_context("maia.query", {"query_len": 10}) as root:
        with tracing.span_context("maia.retrieval", {"n_candidates": 2}):
            pass
        root.set_attribute("status", "answered")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"maia.query", "maia.retrieval"}
    assert spans["maia.retrieval"].parent.span_id == spans["maia.query"].context.span_id
    assert dict(spans["maia.retrieval"].attributes)["n_candidates"] == 2
    assert dict(spans["maia.query"].attributes)["status"] == "answered"


def test_otel_attributes_never_carry_pii(monkeypatch):
    """Redacted keys are stripped by sanitize_attributes AND by span_context."""
    from maia import tracing
    from maia.observability import REDACTED_FIELDS

    assert {"query", "text", "chunk_text", "context", "answer"} <= set(REDACTED_FIELDS)

    dirty = {"query": "sensitive?", "text": "chunk", "chunk_text": "c",
             "context": "ctx", "answer": "ans",
             "n_candidates": 2, "has_evidence": True, "top_dense_score": 0.9}
    clean = tracing.sanitize_attributes(dirty)
    assert clean == {"n_candidates": 2, "has_evidence": True, "top_dense_score": 0.9}
    assert tracing.sanitize_attributes(None) == {}

    _otel_settings_on(monkeypatch)
    provider, exporter = _inmemory_provider()
    monkeypatch.setattr(
        tracing, "get_tracer", lambda name="maia.pipeline": provider.get_tracer(name)
    )
    with tracing.span_context("maia.generation",
                              {"answer": "SECRET-ANSWER", "llm_mode": "mock"}):
        pass
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert "answer" not in attrs
    assert attrs["llm_mode"] == "mock"
    assert not any("SECRET-ANSWER" in str(v) for v in attrs.values())


def test_query_emits_otel_stage_spans_without_pii(monkeypatch):
    """query() wiring: 6 stage spans nested under maia.query, no raw text."""
    import maia.pipeline_query as pq
    from maia import tracing

    _otel_settings_on(monkeypatch)
    provider, exporter = _inmemory_provider()
    monkeypatch.setattr(
        tracing, "get_tracer", lambda name="maia.pipeline": provider.get_tracer(name)
    )

    # WHY the name and the value: this is a PII canary, not a credential. The
    # assertion below is that the canary never appears in a span attribute. The
    # previous literal was a credential-shaped placeholder on a variable named
    # `secret`, which tripped gitleaks' generic-api-key rule on entropy alone and
    # turned the secret-scanning gate red. A credential-shaped name on a fake
    # value is the wrong signal twice over: it fails the scanner, and it teaches a
    # reader that this string is the kind of thing that gets committed. The old
    # literal is deliberately not quoted here, because a comment naming it would
    # trip the same rule it was renamed to avoid.
    pii_canary = "MAIA-PII-CANARY-4f9c2e7a"

    class _Retriever:
        tenant_id = "default"

        def retrieve(self, q, tenant_id=None, session_id=None):
            return [{
                "chunk_id": "c1",
                "text": f"policy text {pii_canary}",
                "metadata": {"filename": "policy.md", "page": 1, "section": "leave"},
                "dense_score": 0.9, "bm25_score": 0.5, "fused_score": 0.8,
            }]

    class _Reranker:
        mode = "score"

        def rerank(self, q, cands, top_k=3):
            return [dict(c, rerank_score=0.95) for c in cands[:top_k]]

    class _LLM:
        mode = "mock"

        def chat(self, messages):
            return f"mock answer {pii_canary}"

    monkeypatch.setattr(
        pq, "build_stack",
        lambda tenant_id=None: (None, None, _Retriever(), _Reranker(), _LLM()),
    )

    res = pq.query(f"chính sách nghỉ phép? {pii_canary}", top_k_final=3)
    assert res["has_evidence"] is True

    spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    expected = {"maia.query", "maia.retrieval", "maia.rerank", "maia.evidence_gate",
                "maia.guardrail", "maia.generation", "maia.response"}
    assert set(by_name) == expected
    root_id = by_name["maia.query"].context.span_id
    for name in expected - {"maia.query"}:
        assert by_name[name].parent.span_id == root_id, name
    # No raw question / chunk / answer text anywhere in span attributes.
    for s in spans:
        for k, v in dict(s.attributes or {}).items():
            assert k not in ("query", "text", "chunk_text", "context", "answer"), (s.name, k)
            assert pii_canary not in str(v), (s.name, k)
    assert dict(by_name["maia.query"].attributes)["status"] == "answered"


def test_query_still_works_with_otel_disabled(monkeypatch):
    """NoOp path: query() result identical whether OTEL is on or off."""
    import maia.pipeline_query as pq

    _otel_settings_off(monkeypatch)

    class _Retriever:
        tenant_id = "default"

        def retrieve(self, q, tenant_id=None, session_id=None):
            return []

    class _Reranker:
        mode = "score"

        def rerank(self, q, cands, top_k=3):
            return []

    class _LLM:
        mode = "mock"

        def chat(self, messages):  # pragma: no cover
            raise AssertionError("no-evidence path must not call the LLM")

    monkeypatch.setattr(
        pq, "build_stack",
        lambda tenant_id=None: (None, None, _Retriever(), _Reranker(), _LLM()),
    )
    res = pq.query("câu hỏi không có bằng chứng?", top_k_final=3)
    assert res["has_evidence"] is False
