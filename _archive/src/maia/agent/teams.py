"""Multi-agent team orchestration over MAIA's existing RAG stack.

No new framework: roles are thin prompt-role wrappers around the same
retriever/reranker/LLM the single agent uses, sharing one retrieval packet
and one trace:

    researcher -> retrieve + grade evidence packet
    analyst    -> synthesize findings from the packet (cited)
    writer     -> compose the final answer with [S#] citations
    reviewer   -> validate citations + grounding; one revision round max

Routing is sequential ("swarm"/parallel left for future work). Every step
degrades gracefully: a failed member yields a flagged partial result rather
than an exception. Invoked explicitly (API/CLI), never inside chat().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..loops.answer_loop import CitationChecker, GroundingChecker
from ..prompt import assemble, build_agent_messages

TEAM_ROLES = ("researcher", "analyst", "writer", "reviewer")

_ROLE_SYSTEM = {
    "researcher": ("You are the RESEARCHER. Summarize ONLY what the retrieved "
                   "context supports, with [S1], [S2] markers. No conclusions."),
    "analyst": ("You are the ANALYST. Turn researcher findings into key points, "
                "each backed by [S#] citations from the context. No new facts."),
    "writer": ("You are the WRITER. Write a concise final answer from the analyst "
               "points, keeping every [S#] citation. Friendly, professional tone."),
    "reviewer": ("You are the REVIEWER. Reply with exactly one word: PASS if every "
                 "[S#] citation exists in the context and claims are supported, "
                 "else FAIL."),
}


@dataclass
class MemberOutput:
    role: str
    text: str
    ok: bool = True
    note: str = ""


@dataclass
class TeamResult:
    question: str
    final_answer: str
    citations: list[dict] = field(default_factory=list)
    members: list[MemberOutput] = field(default_factory=list)
    status: str = "completed"  # completed | partial | refused
    trace: list[dict] = field(default_factory=list)


class AgentTeam:
    """Sequential researcher->analyst->writer->reviewer team."""

    def __init__(self, retriever=None, reranker=None, llm=None,
                 members: list[str] | None = None, tenant_id: str | None = None,
                 max_revision_rounds: int = 1):
        self.retriever = retriever
        self.reranker = reranker
        self.llm = llm
        self.members = [m for m in (members or list(TEAM_ROLES)) if m in TEAM_ROLES] or ["writer"]
        self.tenant_id = tenant_id or settings.TENANT_ID
        self.max_revision_rounds = max(0, max_revision_rounds)
        self._grounding = GroundingChecker(threshold=settings.AGENT_GROUNDING_THRESHOLD)

    def _ensure_stack(self):
        if self.llm is not None and self.retriever is not None:
            return
        from ..pipeline_query import build_stack
        _, _, retriever, reranker, llm = build_stack()
        self.retriever = self.retriever or retriever
        self.reranker = self.reranker or reranker
        self.llm = self.llm or llm

    def _say(self, role: str, user_content: str) -> str:
        messages = [{"role": "system", "content": _ROLE_SYSTEM[role]},
                    {"role": "user", "content": user_content}]
        return self.llm.chat(messages)

    def run(self, question: str, top_k_final: int | None = None) -> TeamResult:
        self._ensure_stack()
        top_k_final = top_k_final or settings.TOP_K_FINAL
        trace: list[dict] = []
        members: list[MemberOutput] = []
        try:
            # researcher: single retrieval packet for the whole team
            candidates = self.retriever.retrieve(question, tenant_id=self.tenant_id)
            reranked = self.reranker.rerank(question, candidates, top_k=top_k_final)
            context, used = assemble(reranked)
            trace.append({"step": "researcher_retrieve", "chunks": len(used)})
            if not used:
                return TeamResult(question, "Không tìm thấy bằng chứng liên quan "
                                  "trong knowledge base. Tôi không thể trả lời chắc chắn.",
                                  [], members, "refused", trace)
            tags = " ".join(c.get("cite_tag", f"[S{i + 1}]") for i, c in enumerate(used))

            def member(role: str, content: str) -> str:
                try:
                    text = self._say(role, content)
                    members.append(MemberOutput(role, text, True))
                    trace.append({"step": role, "chars": len(text)})
                    return text
                except Exception as e:  # degrade: flagged partial, keep going
                    members.append(MemberOutput(role, "", False, f"{type(e).__name__}: {e}"))
                    trace.append({"step": role, "error": True})
                    return ""

            findings = member("researcher", f"Context:\n{context}\n\nQuestion: {question}") \
                if "researcher" in self.members else ""
            analysis = member(
                "analyst",
                f"Context:\n{context}\n\nResearcher findings:\n{findings or '(skipped)'}\n\n"
                f"Question: {question}") if "analyst" in self.members else (findings or "")
            draft = member(
                "writer",
                f"Context:\n{context}\n\nAnalysis:\n{analysis or '(skipped)'}\n\n"
                f"Question: {question}\nAvailable citations: {tags}") \
                if "writer" in self.members else (analysis or "")
            if not draft:
                draft = analysis or findings or "Không tổng hợp được câu trả lời."

            # reviewer: citation + grounding check, one revision round max
            if "reviewer" in self.members:
                for rnd in range(self.max_revision_rounds + 1):
                    cc = CitationChecker(used)
                    cites_valid, _ = cc.check(draft)
                    grounded, score = self._grounding.check(draft, context)
                    verdict = ""
                    try:
                        verdict = self._say(
                            "reviewer",
                            f"Context:\n{context}\n\nAnswer to review:\n{draft}").strip().upper()
                    except Exception:
                        verdict = ""
                    trace.append({"step": "reviewer", "round": rnd, "verdict": verdict,
                                  "cites_valid": cites_valid, "grounding": round(score, 4)})
                    members.append(MemberOutput(
                        "reviewer", verdict or "(skipped)", True,
                        f"cites_valid={cites_valid} grounding={round(score, 4)}"))
                    if ("PASS" in verdict or (cites_valid and grounded)
                            or getattr(self.llm, "mode", "mock") == "mock"):
                        break
                    if rnd < self.max_revision_rounds and "writer" in self.members:
                        draft = member(
                            "writer",
                            f"Context:\n{context}\n\nPrevious draft failed review "
                            f"(cites_valid={cites_valid}). Rewrite using ONLY valid "
                            f"[S#] citations from: {tags}\n\nQuestion: {question}")
                    else:
                        break

            citations = [{**c, "cite_tag": c.get("cite_tag", f"[S{i + 1}]")}
                         for i, c in enumerate(used)]
            status = "completed" if any(m.ok and m.text for m in members) else "partial"
            try:
                from ..stream.metrics import registry
                registry.inc("maia_team_runs_total")
            except Exception:
                pass
            return TeamResult(question, draft, citations, members, status, trace)
        except Exception as e:
            return TeamResult(question, "Đã xảy ra lỗi khi chạy nhóm agent. Vui lòng thử lại.",
                              [], members, "partial",
                              trace + [{"step": "error", "error": f"{type(e).__name__}: {e}"}])

    def to_dict(self, result: TeamResult) -> dict[str, Any]:
        return {"question": result.question, "answer": result.final_answer,
                "status": result.status,
                "citations": [{"tag": c.get("cite_tag", f"[S{i + 1}]"),
                               "chunk_id": c.get("chunk_id", ""),
                               "filename": (c.get("metadata") or {}).get("filename", ""),
                               "section": (c.get("metadata") or {}).get("section", ""),
                               "text": (c.get("text", "") or "")[:600]}
                              for i, c in enumerate(result.citations)],
                "members": [{"role": m.role, "ok": m.ok, "note": m.note,
                             "text": m.text[:2000]} for m in result.members],
                "trace": result.trace, "tenant_id": self.tenant_id}
