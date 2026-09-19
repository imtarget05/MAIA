"""ResponseBuilder — builds AgentResponseModel instances and typed dict responses.

Extracted from EnterpriseAgent._respond, _cards, _ev_summary, _crag_block,
_insufficient, _generate_grounded, _clean_display, _grounding_and_citation.
"""
from __future__ import annotations

from ..answer_format import clean_answer
from ..config import settings
from ..loops.answer_loop import CitationChecker
from ..prompt import build_agent_messages
from .schemas import (
    INSUFFICIENT_TEXT,
    AgentResponseModel,
    Citation,
    EvidenceSummary,
    GroundingInfo,
    chunk_to_citation,
)

_STRICT_RULE = (
    "\n\n[RULE] Answer ONLY from the Context above with valid [S#] citations "
    "that exist in the context. If the context lacks the answer, reply exactly: "
    f'"{INSUFFICIENT_TEXT}"'
)


class ResponseBuilder:
    """Builds typed responses from retrieval packets and LLM output.

    Handles: citation cards, evidence summaries, CRAG blocks, grounded
    generation, insufficient-evidence refusal, and response dict assembly.
    """

    def __init__(self, llm, reranker, grounding, output_validator,
                 doc_sanitizer, workflow, session_store, gen=None,
                 requester_email=""):
        self._llm = llm
        self._reranker = reranker
        self._grounding = grounding
        self._output_validator = output_validator
        self._doc_sanitizer = doc_sanitizer
        self._workflow = workflow
        self._session_store = session_store
        self._gen = gen or {}
        self._requester_email = requester_email

    # ---- static helpers ----

    @staticmethod
    def cards(used: list[dict]) -> list[dict]:
        return [chunk_to_citation(c, c.get("cite_tag", f"[S{i + 1}]")).model_dump()
                for i, c in enumerate(used)]

    @staticmethod
    def ev_summary(iter_res: dict) -> dict:
        rep = iter_res.get("report")
        return EvidenceSummary(
            attempts=iter_res.get("attempts", 0),
            top_dense=round(rep.top_dense, 4) if rep else 0.0,
            reason=rep.reason if rep else "",
            final_query=iter_res.get("final_query", ""),
            rewritten_queries=iter_res.get("rewritten_queries", []),
        ).model_dump()

    @staticmethod
    def crag_block(iter_res: dict | None) -> dict | None:
        if not iter_res or "crag_action" not in iter_res:
            return None
        return {"action": iter_res.get("crag_action"),
                "grades": iter_res.get("crag_grades", []),
                "trace": iter_res.get("crag_trace", [])}

    @staticmethod
    def clean_display(text: str) -> str:
        try:
            return clean_answer(text)
        except Exception:
            return text

    # ---- core methods ----

    def grounding_and_citation(self, answer: str, context: str,
                                used: list[dict]) -> tuple[bool, bool, float]:
        cc = CitationChecker(used)
        cites_valid, _ = cc.check(answer)
        grounded, score = self._grounding.check(answer, context)
        return cites_valid, grounded, score

    def generate_grounded(self, question: str, context: str, used: list[dict],
                           session_id: str, employee_id: str | None = None,
                           tenant_id: str | None = None) -> tuple[str, bool, float]:
        is_mock = getattr(self._llm, "mode", "mock") == "mock"
        tid = tenant_id or settings.TENANT_ID
        history_text = self._session_store.history_text(session_id, tenant_id=tid)
        history_text = self._doc_sanitizer.sanitize(history_text)[0]
        try:
            from .memory import ltm_context
            ltm = ltm_context(employee_id or settings.DEFAULT_EMPLOYEE_ID,
                              tid, question)
            if ltm:
                ltm = self._doc_sanitizer.sanitize(ltm)[0]
                history_text = f"{ltm}\n\n{history_text}" if history_text else ltm
        except Exception:
            pass
        answer = self._llm.chat(build_agent_messages(question, context, history_text),
                                **self._gen)
        cites_valid, grounded, score = self.grounding_and_citation(answer, context, used)
        if cites_valid and (grounded or is_mock):
            return self.clean_display(answer), cites_valid, score
        answer2 = self._llm.chat(
            build_agent_messages(question, context + _STRICT_RULE, history_text),
            **self._gen)
        cites_valid2, grounded2, score2 = self.grounding_and_citation(answer2, context, used)
        if cites_valid2 and (grounded2 or is_mock):
            return self.clean_display(answer2), cites_valid2, score2
        return INSUFFICIENT_TEXT, True, 0.0

    def respond(self, model: AgentResponseModel, *, has_evidence: bool,
                 flags: list, plan: dict, rewritten_query: str,
                 tenant_id: str, iter_res: dict | None = None) -> dict:
        out = model.model_dump()
        out.update({
            "has_evidence": has_evidence,
            "cites_valid": model.grounding.cites_valid,
            "grounding_score": model.grounding.score,
            "needs_clarification": model.needs_clarification,
            "flags": flags,
            "plan": plan,
            "rewritten_query": rewritten_query,
            "llm_mode": self._llm.mode,
            "rerank_mode": self._reranker.mode,
            "tenant_id": tenant_id,
        })
        crag = self.crag_block(iter_res)
        if crag is not None:
            out["crag"] = crag
        return out

    def insufficient(self, intent: str, slots: dict, flags: list, plan: dict,
                      rewritten_query: str, iter_res: dict, tenant_id: str,
                      session_id: str, question: str) -> dict:
        used = iter_res.get("used", [])
        retrieved = self.cards(used)
        self._session_store.append(session_id, "user", question, intent, tenant_id=tenant_id)
        self._session_store.append(session_id, "assistant", INSUFFICIENT_TEXT, intent, tenant_id=tenant_id)
        model = AgentResponseModel(
            status="insufficient_evidence",
            answer=INSUFFICIENT_TEXT,
            intent=intent,
            citations=[],
            retrieved=[Citation(**c) for c in retrieved],
            evidence=EvidenceSummary(**self.ev_summary(iter_res)),
            grounding=GroundingInfo(supported=False, score=0.0, cites_valid=True),
            slots=slots,
        )
        return self.respond(model, has_evidence=False, flags=flags, plan=plan,
                             rewritten_query=rewritten_query, tenant_id=tenant_id,
                             iter_res=iter_res)
