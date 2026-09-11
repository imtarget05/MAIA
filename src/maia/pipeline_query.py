"""Query pipeline (§5): question -> embed -> hybrid retrieve -> rerank
-> context assembly -> LLM -> grounded answer + citations (§10 outputs 4,5,6)."""
import time

from .config import settings
from .embeddings import get_embedder
from .ingestion_pipeline import (
    ingest_data_dir,  # noqa: F401  (canonical ingest entry — re-exported; tests + CI import it from here)
)
from .llm import CloudflareLLM
from .pipeline_wiring import PipelineTracer
from .prompt import assemble, build_messages
from .reranker import Reranker
from .retriever import HybridRetriever
from .vector_store import QdrantStore


def build_stack(tenant_id: str | None = None):
    # Deploy fix 2026-09-11: reuse the process-wide Embedder singleton.
    # Constructing Embedder() per request re-loads the FastEmbed ONNX model
    # (~hundreds of MB) and OOM-crashes Render free tier (512Mi).
    # Initialise the embedder FIRST so its real dim (1024 for Cloudflare BGE-m3)
    # is known before QdrantStore creates the collection.
    embedder = get_embedder(model=settings.EMBED_MODEL, dim=settings.EMBED_DIM)
    store = QdrantStore(url=settings.QDRANT_URL, collection=settings.QDRANT_COLLECTION,
                        dim=embedder.dim, api_key=settings.QDRANT_API_KEY)
    retriever = HybridRetriever(
        store, embedder, storage_dir=settings.STORAGE_DIR,
        top_k_dense=settings.TOP_K_DENSE, top_k_bm25=settings.TOP_K_BM25,
        top_k_fused=settings.TOP_K_FUSED, rrf_k=settings.RRF_K,
        tenant_id=tenant_id or settings.TENANT_ID,
    )
    reranker = Reranker()
    llm = CloudflareLLM(settings.CLOUDFLARE_ACCOUNT_ID, settings.CLOUDFLARE_API_TOKEN, settings.CLOUDFLARE_MODEL)
    return embedder, store, retriever, reranker, llm


def query(question: str, top_k_final: int | None = None, tenant_id: str | None = None,
          session_id: str | None = None) -> dict:
    # MAIA-07: create tracer at request entry
    tracer = PipelineTracer()
    tracer.log("query_input", query=question, tenant=tenant_id, session=session_id)

    _, _store, retriever, reranker, llm = build_stack(tenant_id=tenant_id)
    top_k_final = top_k_final or settings.TOP_K_FINAL
    candidates = retriever.retrieve(question, tenant_id=retriever.tenant_id,
                                    session_id=session_id)
    tracer.log("retrieval", n_candidates=len(candidates),
               tenant_filter=tenant_id or settings.TENANT_ID)

    reranked = reranker.rerank(question, candidates, top_k=top_k_final)
    tracer.log("rerank", mode=reranker.mode,
               top_scores=[round(c.get("rerank_score", 0), 4) for c in reranked[:3]])

    context, used = assemble(reranked)
    if not used:
        tracer.log("evidence_gate", has_evidence=False, refusal_reason="no_candidates")
        tracer.log("guardrail", ingest_sanitization=True, pii_redaction=True,
                   input_guardrail=False, output_guardrail=False)
        tracer.log("response", status="no_evidence", n_citations=0)
        return _query_response(tracer, llm, reranker, answer="Không tìm thấy bằng chứng liên quan trong knowledge base. Tôi không thể trả lời chắc chắn.",
                               citations=[], has_evidence=False, candidates=[], refused=False)

    # §8.3 missing evidence: threshold on dense score of top-1
    top_dense = max([c.get("dense_score", 0) for c in used], default=0)
    has_evidence = top_dense >= settings.SIMILARITY_THRESHOLD or llm.mode == "mock" and len(used) > 0
    # In mock mode keep evidence True if we retrieved anything (demo-friendly)
    if llm.mode == "mock" and used:
        has_evidence = True
    # MAIA-04: refuse instead of generating when evidence is too weak
    # (non-mock mode). The agent path already does this via its evidence gate.
    if not has_evidence:
        tracer.log("evidence_gate", has_evidence=False, refusal_reason="below_threshold",
                   top_dense_score=round(top_dense, 4), threshold=settings.SIMILARITY_THRESHOLD)
        tracer.log("guardrail", ingest_sanitization=True, pii_redaction=True,
                   input_guardrail=False, output_guardrail=False)
        tracer.log("response", status="refused", n_citations=0)
        return _query_response(tracer, llm, reranker, answer="Không tìm thấy bằng chứng liên quan trong knowledge base. Tôi không thể trả lời chắc chắn.",
                               citations=[], has_evidence=False, candidates=used, refused=True,
                               extra={"top_dense_score": round(top_dense, 4)})

    tracer.log("evidence_gate", has_evidence=True, top_dense_score=round(top_dense, 4),
               threshold=settings.SIMILARITY_THRESHOLD)
    # MAIA-07: guardrail stage. The query() path applies document sanitization
    # + PII redaction at ingest time (G-03/G-04); the agent path adds input/output
    # guardrails per-message. Log which layers are active for this request.
    tracer.log("guardrail", ingest_sanitization=True, pii_redaction=True,
               input_guardrail=False, output_guardrail=False)
    messages = build_messages(question, context)
    t0 = time.time()
    answer = llm.chat(messages)
    # Display-only cleanup: strip echoed boundary tags + redundant
    # 'Sources:' footer (UI renders structured citations separately).
    try:
        from .answer_format import clean_answer
        answer = clean_answer(answer)
    except Exception:
        pass
    tracer.log("generation", llm_mode=llm.mode, latency_ms=round((time.time() - t0) * 1000, 1))

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
    return _query_response(tracer, llm, reranker, answer=answer, citations=citations,
                           has_evidence=True, candidates=used, refused=False)


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
