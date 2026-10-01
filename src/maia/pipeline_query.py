"""Query pipeline (§5): question -> embed -> hybrid retrieve -> rerank
-> context assembly -> LLM -> grounded answer + citations (§10 outputs 4,5,6)."""
import time

from . import tracing as _tracing
from .config import settings
from .embeddings import get_embedder
from .ingestion_pipeline import (
    ingest_data_dir,  # noqa: F401  (canonical ingest entry — re-exported; tests + CI import it from here)
)
from .llm import (  # noqa: F401 - test seam: patched by name in test_health_endpoints
    CloudflareLLM,
    LocalOpenAICompatLLM,
    build_llm,
)
from .loops.metrics import registry
from .pipeline_wiring import PipelineTracer
from .prompt import assemble, build_messages
from .reranker import get_reranker
from .retrieval_backends import build_vector_store
from .retriever import HybridRetriever
from .vector_store import QdrantStore

# Byte JD evidence: rough cost model for local-vs-commercial comparison.
# Local LAN inference costs ~$0; commercial reference = GPT-4o-mini $0.15/1M in.
_COMMERCIAL_PER_1K_USD = 0.00015


def _est_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def build_stack(tenant_id: str | None = None):
    # Deploy fix 2026-09-11: reuse the process-wide Embedder singleton.
    # Constructing Embedder() per request re-loads the FastEmbed ONNX model
    # (~hundreds of MB) and OOM-crashes Render free tier (512Mi).
    # Initialise the embedder FIRST so its real dim (1024 for Cloudflare BGE-m3)
    # is known before QdrantStore creates the collection.
    embedder = get_embedder(model=settings.EMBED_MODEL, dim=settings.EMBED_DIM)
    # Backend dispatch lives in retrieval_backends.build_vector_store.
    # qdrant_store_cls=QdrantStore reads this module's own symbol at call
    # time, so patch("maia.pipeline_query.QdrantStore") keeps intercepting
    # construction in tests (default backend wraps the real store in a
    # behavior-preserving QdrantAdapter).
    store = build_vector_store(dim=embedder.dim,
                               qdrant_store_cls=QdrantStore)
    retriever = HybridRetriever(
        store, embedder, storage_dir=settings.STORAGE_DIR,
        top_k_dense=settings.TOP_K_DENSE, top_k_bm25=settings.TOP_K_BM25,
        top_k_fused=settings.TOP_K_FUSED, rrf_k=settings.RRF_K,
        tenant_id=tenant_id or settings.TENANT_ID,
    )
    # Lifecycle fix: the cross-encoder is ~90MB and Reranker() re-loads it.
    # Constructing it per query() made every request pay a full model load
    # (~30s here) before any retrieval happened. get_reranker() caches per
    # model name, so a warm process constructs it at most once.
    reranker = get_reranker()
    # LLM backend from settings.LLM_PROVIDER: local (LAN LM Studio, default),
    # cloudflare, or mock. See maia.llm.build_llm.
    llm = build_llm()
    return embedder, store, retriever, reranker, llm


def query(question: str, top_k_final: int | None = None, tenant_id: str | None = None,
          session_id: str | None = None) -> dict:
    """Request entry: opens the OTEL root span, then runs the staged pipeline.

    The root span (``maia.query``) carries structural attributes only
    (lengths, tenant, flags) — never the raw question. Each pipeline stage
    inside :func:`_query_impl` is a child span. When OTEL is disabled
    (default) all spans are NoOp and behaviour is unchanged.
    """
    with _tracing.span_context("maia.query", {
        "query_len": len(question or ""),
        "tenant_filter": tenant_id or settings.TENANT_ID,
        "has_session": bool(session_id),
        "top_k_final": top_k_final or settings.TOP_K_FINAL,
    }) as _root:
        try:
            resp = _query_impl(question, top_k_final=top_k_final,
                               tenant_id=tenant_id, session_id=session_id)
        except Exception:
            try:
                _root.set_attribute("error", True)
            except Exception:
                pass
            raise
        try:
            if resp.get("refused"):
                _status = "refused"
            elif not resp.get("has_evidence"):
                _status = "no_evidence"
            else:
                _status = "answered"
            _root.set_attribute("status", _status)
            _root.set_attribute("n_citations", len(resp.get("citations") or []))
            _root.set_attribute("llm_mode", str(resp.get("llm_mode", "")))
            _root.set_attribute("rerank_mode", str(resp.get("rerank_mode", "")))
        except Exception:
            pass
        return resp


def _query_impl(question: str, top_k_final: int | None = None, tenant_id: str | None = None,
                session_id: str | None = None) -> dict:
    # MAIA-07: create tracer at request entry
    t_start = time.time()
    tracer = PipelineTracer()
    tracer.log("query_input", query=question, tenant=tenant_id, session=session_id)

    _, _store, retriever, reranker, llm = build_stack(tenant_id=tenant_id)
    top_k_final = top_k_final or settings.TOP_K_FINAL
    with _tracing.span_context("maia.retrieval", {
        "tenant_filter": tenant_id or settings.TENANT_ID,
        "top_k_fused": settings.TOP_K_FUSED,
    }) as _span:
        candidates = retriever.retrieve(question, tenant_id=retriever.tenant_id,
                                        session_id=session_id)
        _span.set_attribute("n_candidates", len(candidates))
    tracer.log("retrieval", n_candidates=len(candidates),
               tenant_filter=tenant_id or settings.TENANT_ID)

    with _tracing.span_context("maia.rerank", {
        "mode": reranker.mode,
        "top_k_final": top_k_final,
    }) as _span:
        reranked = reranker.rerank(question, candidates, top_k=top_k_final)
        _span.set_attribute("n_reranked", len(reranked))
        _top_scores = [round(c.get("rerank_score", 0), 4) for c in reranked[:3]]
        _span.set_attribute("top_scores", _top_scores)
    tracer.log("rerank", mode=reranker.mode,
               top_scores=[round(c.get("rerank_score", 0), 4) for c in reranked[:3]])

    context, used = assemble(reranked)
    with _tracing.span_context("maia.evidence_gate", {
        "threshold": settings.SIMILARITY_THRESHOLD,
    }) as _gate:
        if not used:
            _gate.set_attribute("has_evidence", False)
            _gate.set_attribute("refusal_reason", "no_candidates")
            tracer.log("evidence_gate", has_evidence=False, refusal_reason="no_candidates")
            with _tracing.span_context("maia.guardrail", {
                "ingest_sanitization": True, "pii_redaction": True,
                "input_guardrail": False, "output_guardrail": False,
            }):
                tracer.log("guardrail", ingest_sanitization=True, pii_redaction=True,
                           input_guardrail=False, output_guardrail=False)
            with _tracing.span_context("maia.response", {"status": "no_evidence", "n_citations": 0}):
                tracer.log("response", status="no_evidence", n_citations=0)
                _track_ai_query(question, "", [], refused=True, latency_s=time.time() - t_start)
                return _query_response(tracer, llm, reranker, answer="Không tìm thấy bằng chứng liên quan trong knowledge base. Tôi không thể trả lời chắc chắn.",
                                       citations=[], has_evidence=False, candidates=[], refused=False)

        # §8.3 missing evidence: threshold on dense score of top-1
        top_dense = max([c.get("dense_score", 0) for c in used], default=0)
        has_evidence = top_dense >= settings.SIMILARITY_THRESHOLD or llm.mode == "mock" and len(used) > 0
        # In mock mode keep evidence True if we retrieved anything (demo-friendly)
        if llm.mode == "mock" and used:
            has_evidence = True
        _gate.set_attribute("has_evidence", bool(has_evidence))
        _gate.set_attribute("top_dense_score", round(top_dense, 4))
        # MAIA-04: refuse instead of generating when evidence is too weak
        # (non-mock mode). The agent path already does this via its evidence gate.
        if not has_evidence:
            _gate.set_attribute("refusal_reason", "below_threshold")
            tracer.log("evidence_gate", has_evidence=False, refusal_reason="below_threshold",
                       top_dense_score=round(top_dense, 4), threshold=settings.SIMILARITY_THRESHOLD)
            with _tracing.span_context("maia.guardrail", {
                "ingest_sanitization": True, "pii_redaction": True,
                "input_guardrail": False, "output_guardrail": False,
            }):
                tracer.log("guardrail", ingest_sanitization=True, pii_redaction=True,
                           input_guardrail=False, output_guardrail=False)
            with _tracing.span_context("maia.response", {"status": "refused", "n_citations": 0}):
                tracer.log("response", status="refused", n_citations=0)
                _track_ai_query(question, "", [], refused=True, latency_s=time.time() - t_start)
                return _query_response(tracer, llm, reranker, answer="Không tìm thấy bằng chứng liên quan trong knowledge base. Tôi không thể trả lời chắc chắn.",
                                       citations=[], has_evidence=False, candidates=used, refused=True,
                                       extra={"top_dense_score": round(top_dense, 4)})

        tracer.log("evidence_gate", has_evidence=True, top_dense_score=round(top_dense, 4),
                   threshold=settings.SIMILARITY_THRESHOLD)
    # MAIA-07: guardrail stage. The query() path applies document sanitization
    # + PII redaction at ingest time (G-03/G-04); the agent path adds input/output
    # guardrails per-message. Log which layers are active for this request.
    with _tracing.span_context("maia.guardrail", {
        "ingest_sanitization": True, "pii_redaction": True,
        "input_guardrail": False, "output_guardrail": False,
    }):
        tracer.log("guardrail", ingest_sanitization=True, pii_redaction=True,
                   input_guardrail=False, output_guardrail=False)
    messages = build_messages(question, context)
    with _tracing.span_context("maia.generation", {"llm_mode": llm.mode}) as _span:
        t0 = time.time()
        answer = llm.chat(messages)
        # Display-only cleanup: strip echoed boundary tags + redundant
        # 'Sources:' footer (UI renders structured citations separately).
        try:
            from .answer_format import clean_answer
            answer = clean_answer(answer)
        except Exception:
            pass
        _span.set_attribute("latency_ms", round((time.time() - t0) * 1000, 1))
        _span.set_attribute("answer_len", len(answer or ""))
    tracer.log("generation", llm_mode=llm.mode, latency_ms=round((time.time() - t0) * 1000, 1))

    # Presentation layer: citations[].text is a 600-char excerpt for the UI.
    # It is deliberately NOT the evaluation evidence -- maia.eval._canonical_texts
    # measures metrics against the full chunk text in res["candidates"].
    citations = [
        {"tag": c.get("cite_tag", f"[S{i+1}]"), "chunk_id": c["chunk_id"],
         "filename": c["metadata"].get("filename", ""), "page": c["metadata"].get("page", ""),
         "section": c["metadata"].get("section", ""),
         "dense_score": c.get("dense_score", 0.0), "fused_score": c.get("fused_score", 0.0),
         "rerank_score": c.get("rerank_score", 0.0),
         "text": c["text"][:600]}
        for i, c in enumerate(used)
    ]
    tracer.log("response", status="answered", n_citations=len(citations))
    with _tracing.span_context("maia.response", {
        "status": "answered", "n_citations": len(citations),
    }):
        _track_ai_query(question, answer, citations, refused=False, latency_s=time.time() - t_start)
        return _query_response(tracer, llm, reranker, answer=answer, citations=citations,
                               has_evidence=True, candidates=used, refused=False)


def _track_ai_query(question: str, answer: str, citations: list, *, refused: bool, latency_s: float) -> None:
    """Byte JD evidence: per-query reliability accounting (latency/tokens/cost/grounding)."""
    try:
        registry.observe_latency(latency_s)
        registry.inc("maia_queries_total")
        if refused:
            registry.inc("maia_query_refusals_total")
        else:
            registry.inc("maia_query_answers_total")
        n_cit = len(citations or [])
        registry.inc("maia_citations_total", by=float(n_cit))
        tokens = _est_tokens(question) + _est_tokens(answer) + sum(_est_tokens(c.get("text", "")) for c in (citations or []))
        registry.inc("maia_tokens_total", by=float(tokens))
        registry.inc("maia_cost_saved_usd_total", by=tokens / 1000.0 * _COMMERCIAL_PER_1K_USD)
    except Exception:
        pass


def _query_response(tracer: PipelineTracer, llm, reranker, *, answer, citations,
                    has_evidence, candidates, refused, extra=None) -> dict:
    """Build the query response dict, embedding _trace if PIPELINE_TRACE is enabled."""
    resp = {"answer": answer, "citations": citations, "has_evidence": has_evidence,
            "candidates": candidates, "llm_mode": llm.mode, "rerank_mode": reranker.mode}
    if refused:
        resp["refused"] = True
    if extra:
        resp.update(extra)
    # MAIA-07: embed redacted _trace when enabled
    if settings.PIPELINE_TRACE:
        resp["_trace"] = tracer.finalize_redacted()
    return resp
