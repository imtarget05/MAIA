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


@dataclass
class EvidenceReport:
    enough: bool
    has_vectors: bool
    top_dense: float
    grounding_score: float
    reason: str
    used: list[dict]
    context: str


def evidence_check(used: list[dict], context: str, grounding_score: float | None = None) -> EvidenceReport:
    if not used:
        return EvidenceReport(False, False, 0.0, 0.0, "no_vectors", used, context)
    top_dense = max([c.get("dense_score", 0) for c in used], default=0.0)
    # threshold from config
    dense_ok = top_dense >= settings.SIMILARITY_THRESHOLD or top_dense >= settings.AGENT_EVIDENCE_THRESHOLD
    # grounding proxy if provided
    if grounding_score is not None:
        ground_ok = grounding_score >= settings.AGENT_GROUNDING_THRESHOLD
        enough = dense_ok and ground_ok
        reason = "ok" if enough else ("ground_low" if not ground_ok else "dense_low")
        return EvidenceReport(enough, True, top_dense, grounding_score, reason, used, context)
    enough = dense_ok
    reason = "ok" if enough else "dense_low"
    return EvidenceReport(enough, True, top_dense, 0.0, reason, used, context)


class AgenticRetriever:
    """Iterative retriever with self-correction."""
    def __init__(self, retriever, reranker, session_store=None):
        self.retriever = retriever
        self.reranker = reranker
        self.session_store = session_store
        self.grounding = GroundingChecker(threshold=settings.AGENT_GROUNDING_THRESHOLD)

    def _retrieve_once(self, query: str, tenant_id: str | None, top_k_final: int | None) -> tuple[str, list[dict]]:
        top_k_final = top_k_final or settings.TOP_K_FINAL
        candidates = self.retriever.retrieve(query, tenant_id=tenant_id)
        reranked = self.reranker.rerank(query, candidates, top_k=top_k_final)
        context, used = assemble(reranked)
        return context, used

    def iterative_retrieve(self, question: str, session_id: str = "default", tenant_id: str | None = None, top_k_final: int | None = None) -> dict:
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
        # self-correction variants: add domain keywords if evidence low
        expansions = [
            base_q,
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
            context, used = self._retrieve_once(q, tenant_id, top_k_final)
            # evidence check (dense only first)
            report = evidence_check(used, context)
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
