"""Agentic RAG loop — iterative retrieval + evidence check + self-correction (§16-19).

Implements the core Agentic RAG diagram:
                Knowledge Agent
                     ↓ Decide
       ┌─────────────┼─────────────┐
       ↓             ↓             ↓
    Retrieve      Memory         Tool
       ↓             ↓             ↓
    Rerank       Context        API/DB
       └─────────────┼─────────────┘
                     ↓ Evidence Check → Enough? → Generate → Grounding → Citation
                                  No → Retrieve again (query rewrite)

Unlike single-shot RAG, agent may loop Retrieve→Rerank→Evidence up to AGENT_MAX_ITER
with query-rewrite from short-term memory.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..loops.answer_loop import GroundingChecker
from ..prompt import assemble
from ..textnorm import topical_overlap

# Minimum expanded-token overlap between question and context for the
# evidence to count as topically supporting (cross-lingual via synonyms).
TOPICAL_MIN_OVERLAP = 0.05


@dataclass
class EvidenceReport:
    enough: bool
    has_vectors: bool
    top_dense: float
    grounding_score: float
    reason: str
    used: list[dict]
    context: str


def evidence_check(used: list[dict], context: str, grounding_score: float | None = None,
                   query: str = "") -> EvidenceReport:
    """Dense threshold AND topical support.

    A high dense cosine alone is not enough: cross-lingual dense scores
    cluster in a narrow band, so we additionally require the retrieved
    context to share (synonym-expanded) tokens with the question —
    or at least one BM25 hit. Otherwise reason="topical_low".
    """
    if not used:
        return EvidenceReport(False, False, 0.0, 0.0, "no_vectors", used, context)
    top_dense = max([c.get("dense_score", 0) for c in used], default=0.0)
    # threshold from config
    dense_ok = top_dense >= settings.SIMILARITY_THRESHOLD or top_dense >= settings.AGENT_EVIDENCE_THRESHOLD
    topical = True
    if query:
        has_bm25 = any((c.get("bm25_score") or 0) > 0 for c in used)
        topical = has_bm25 or topical_overlap(query, context) >= TOPICAL_MIN_OVERLAP
    if not dense_ok:
        reason = "dense_low"
        enough = False
    elif not topical:
        reason = "topical_low"
        enough = False
    else:
        reason = "ok"
        enough = True
    # grounding proxy if provided
    if grounding_score is not None:
        ground_ok = grounding_score >= settings.AGENT_GROUNDING_THRESHOLD
        if not ground_ok:
            return EvidenceReport(False, True, top_dense, grounding_score, "ground_low", used, context)
        return EvidenceReport(enough, True, top_dense, grounding_score, reason, used, context)
    return EvidenceReport(enough, True, top_dense, 0.0, reason, used, context)


class AgenticRetriever:
    """Iterative retriever with self-correction."""
    def __init__(self, retriever, reranker, session_store=None):
        self.retriever = retriever
        self.reranker = reranker
        self.session_store = session_store
        self.grounding = GroundingChecker(threshold=settings.AGENT_GROUNDING_THRESHOLD)

    def _retrieve_once(self, query: str, tenant_id: str | None, top_k_final: int | None,
                         session_id: str | None = None) -> tuple[str, list[dict]]:
        top_k_final = top_k_final or settings.TOP_K_FINAL
        candidates = self.retriever.retrieve(query, tenant_id=tenant_id, session_id=session_id)
        reranked = self.reranker.rerank(query, candidates, top_k=top_k_final)
        context, used = assemble(reranked)
        return context, used

    def corrective_retrieve(self, question: str, session_id: str = "default",
                              tenant_id: str | None = None,
                              top_k_final: int | None = None,
                              llm: Any | None = None) -> dict:
        """CRAG retrieval: grade -> refine/rewrite -> optional web fallback.

        Same dict shape as iterative_retrieve (context, used, attempts,
        report, rewritten_queries, final_query) plus crag_action, crag_grades,
        crag_trace. Falls back to iterative_retrieve unless CRAG_ENABLED.
        """
        if not settings.CRAG_ENABLED:
            return self.iterative_retrieve(question, session_id, tenant_id, top_k_final)
        from ..loops.corrective_rag import CorrectiveRetriever, RetrievalGrader
        from .session import session_store as _default_store
        store = self.session_store or _default_store

        base_q = question
        mem_q = store.rewrite_query(session_id, question) if store else question
        rewritten = [mem_q] if mem_q != base_q else []
        crag = CorrectiveRetriever(
            retriever=self.retriever, reranker=self.reranker, assemble_fn=assemble,
            grader=RetrievalGrader(llm=llm), llm=llm)
        result = crag.retrieve_corrective(mem_q, tenant_id=tenant_id, top_k_final=top_k_final,
                                            session_id=session_id)
        top_dense = max([c.get("dense_score", 0) for c in result.used], default=0.0)
        enough = result.action in ("correct", "refined")
        report = EvidenceReport(
            enough=enough, has_vectors=bool(result.used), top_dense=top_dense,
            grounding_score=0.0,
            reason=f"crag:{result.action}" if enough else result.action,
            used=result.used, context=result.context)
        return {
            "context": result.context, "used": result.used,
            "attempts": result.attempts, "report": report,
            "rewritten_queries": (
                rewritten + [s.get("query", "") for s in result.trace if s.get("step") == "rewrite"]),
            "final_query": mem_q,
            "crag_action": result.action,
            "crag_grades": [{"chunk_id": g.chunk_id, "label": g.label.value,
                             "confidence": g.confidence, "reason": g.reason}
                            for g in result.grades],
            "crag_trace": result.trace,
        }

    def iterative_retrieve(self, question: str, session_id: str = "default", tenant_id: str | None = None,
                           top_k_final: int | None = None, expansions: list[str] | None = None) -> dict:
        """Loop: retrieve → evidence check → rewrite → retry.
        Returns {context, used, attempts, report, rewritten_queries}
        """
        from .session import session_store as _default_store
        store = self.session_store or _default_store

        attempts = 0
        rewritten = []
        last_report: EvidenceReport | None = None
        context, used = "", []

        # Build query variants: original → memory-rewritten → expanded
        base_q = question
        mem_q = store.rewrite_query(session_id, question) if store else question
        variants = [mem_q]
        if mem_q != base_q:
            rewritten.append(mem_q)
        # self-correction variants: intent-aware synonym expansion first
        # (Vietnamese queries ↔ English docs), then generic EN fallbacks.
        expansions = expansions or []
        expansions = [
            base_q,
            *(f"{base_q} {hint}" for hint in expansions),
            f"{base_q} policy procedure",
            f"{base_q} Section",
        ]
        # ensure unique ordered
        variants_expanded = []
        for v in variants + expansions:
            if v not in variants_expanded:
                variants_expanded.append(v)

        max_iter = max(1, settings.AGENT_MAX_ITER)
        for i in range(min(max_iter, len(variants_expanded))):
            q = variants_expanded[i]
            context, used = self._retrieve_once(q, tenant_id, top_k_final, session_id)
            # evidence check (dense + topical support against the ORIGINAL question)
            report = evidence_check(used, context, query=base_q)
            last_report = report
            attempts = i + 1
            if report.enough:
                return {"context": context, "used": used, "attempts": attempts, "report": report, "rewritten_queries": rewritten, "final_query": q}
            # if not enough, keep loop; record rewritten for trace
            if q not in rewritten and q != base_q:
                rewritten.append(q)

        # final report after exhausting
        last_report = last_report or EvidenceReport(False, False, 0.0, 0.0, "no_attempt", [], "")
        return {"context": context, "used": used, "attempts": attempts, "report": last_report, "rewritten_queries": rewritten, "final_query": variants_expanded[attempts-1] if attempts else base_q}
