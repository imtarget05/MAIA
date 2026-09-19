"""Typed response contract for MAIA Agentic RAG (P1).

Every agent answer is a typed object — never a bare string:

    status: answered | insufficient_evidence | needs_approval
          | needs_clarification | action_completed | action_cancelled | error
    answer: human-readable text (may be None for pure refusal states)
    citations: chunks the answer actually cites ([S1]..[Sn], validated)
    retrieved: chunks retrieved but NOT sufficient (insufficient_evidence only)
    evidence: single retrieval packet trace (attempts, top_dense, reason, queries)
    grounding: {supported, score, cites_valid}
    action: completed side-effect result (only after explicit approval)
    pending_action: proposed side-effect awaiting human approval (C1)

The same retrieval packet feeds both answer generation and the citation
payload, so answer and citations can never diverge.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

INSUFFICIENT_TEXT = (
    "Không tìm thấy bằng chứng liên quan trong knowledge base. "
    "Tôi không thể trả lời chắc chắn."
)


ResponseStatus = Literal[
    "answered",
    "insufficient_evidence",
    "needs_approval",
    "needs_clarification",
    "action_completed",
    "action_cancelled",
    "error",
]

Relevance = Literal["high", "medium", "low"]

# Raw-score → human label thresholds (dense cosine).
RELEVANCE_HIGH = 0.65
RELEVANCE_MEDIUM = 0.40


def relevance_for(dense_score: float) -> Relevance:
    """Map a raw dense score to a user-facing relevance label."""
    try:
        s = float(dense_score)
    except (TypeError, ValueError):
        return "low"
    if s >= RELEVANCE_HIGH:
        return "high"
    if s >= RELEVANCE_MEDIUM:
        return "medium"
    return "low"


class Citation(BaseModel):
    tag: str = "[S1]"
    chunk_id: str = ""
    filename: str = ""
    section: str = ""
    page: str = ""
    text: str = ""  # evidence excerpt (<=600 chars) — first-class provenance
    relevance: Relevance = "low"
    dense_score: float = 0.0
    fused_score: float = 0.0
    rerank_score: float = 0.0


class EvidenceSummary(BaseModel):
    attempts: int = 0
    top_dense: float = 0.0
    reason: str = ""
    final_query: str = ""
    rewritten_queries: list[str] = Field(default_factory=list)


class GroundingInfo(BaseModel):
    supported: bool = False
    score: float = 0.0
    cites_valid: bool = True


class PendingAction(BaseModel):
    type: str = ""  # create_it_ticket | create_leave_request
    params: dict = Field(default_factory=dict)
    summary: str = ""  # human-readable card text for the approval UI


class ActionResult(BaseModel):
    type: str = ""
    status: str = "completed"  # completed | failed | cancelled
    result: dict = Field(default_factory=dict)
    verify: dict | None = None


class AgentResponseModel(BaseModel):
    status: ResponseStatus = "answered"
    answer: str = ""
    intent: str = "general"
    citations: list[Citation] = Field(default_factory=list)
    retrieved: list[Citation] = Field(default_factory=list)
    evidence: EvidenceSummary = Field(default_factory=EvidenceSummary)
    grounding: GroundingInfo = Field(default_factory=GroundingInfo)
    action: ActionResult | None = None
    pending_action: PendingAction | None = None
    slots: dict = Field(default_factory=dict)
    needs_clarification: bool = False
    clarification_question: str | None = None


def chunk_to_citation(c: dict, tag: str) -> Citation:
    """Build a Citation card from a retrieved chunk (single packet → both uses)."""
    meta = c.get("metadata", {}) or {}
    dense = float(c.get("dense_score", 0.0) or 0.0)
    return Citation(
        tag=tag,
        chunk_id=c.get("chunk_id", ""),
        filename=meta.get("filename", ""),
        section=meta.get("section", ""),
        page=meta.get("page", ""),
        text=(c.get("text", "") or "")[:600],
        relevance=relevance_for(dense),
        dense_score=dense,
        fused_score=float(c.get("fused_score", 0.0) or 0.0),
        rerank_score=float(c.get("rerank_score", 0.0) or 0.0),
    )
