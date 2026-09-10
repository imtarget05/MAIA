"""Enterprise Agent — Agentic RAG: Decide → Retrieve/Memory/Tool → Evidence → Self-correction → Grounding.

Response contract (typed, never a bare string):
    answered | insufficient_evidence | needs_approval | needs_clarification
    | action_completed | action_cancelled | error

Side-effect tools (create_it_ticket, create_leave_request) NEVER execute
inside chat(): they return status=needs_approval with a pending_action card.
Execution happens only in confirm_action() after explicit human approval (C1).

One retrieval packet feeds both answer generation and citations, so the
answer and its [S1]..[Sn] markers can never diverge.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
from ..loops.answer_loop import CitationChecker, GroundingChecker
from ..loops.guardrails import (
    DocumentSanitizer,
    InputGuardrail,
    OutputGuardrail,
)
from . import hris as hris_conn
from .action_proposer import ActionProposer
from .intent_router import IntentRouter
from .intents import detect_intent, slots_for_intent
from .response_builder import ResponseBuilder
from .schemas import (
    INSUFFICIENT_TEXT,
    ActionResult,
    AgentResponseModel,
    Citation,
    EvidenceSummary,
    GroundingInfo,
)
from .session import session_store
from .workflow_orchestrator import WorkflowOrchestrator

_INTENT_HINTS: dict[str, list[str]] = {
    "leave_request": ["Leave Policy annual leave request approval"],
    "leave_balance": ["Leave Policy annual leave balance"],
    "hr_policy": ["Leave Policy HR"],
    "security": ["IT Security Policy lost device report Help Desk"],
    "it_help": ["IT Help Desk laptop broken support ext 202"],
    "vpn": ["VPN Guide vpn.company.com access"],
    "expense": ["Expense Policy reimbursement"],
    "benefits": ["Benefits insurance wellness"],
    "onboarding": ["Onboarding Guide SSO first day"],
    "general": [],
}


@dataclass
class AgentResponse:
    answer: str
    intent: str
    citations: list[dict]
    has_evidence: bool
    grounding_score: float
    cites_valid: bool
    action: dict | None = None
    slots: dict = field(default_factory=dict)
    needs_clarification: bool = False
    clarification_question: str | None = None


class EnterpriseAgent:
    def __init__(self, embedder=None, store=None, retriever=None, reranker=None, llm=None, tenant_id: str | None = None):
        self._embedder = embedder
        self._store = store
        self._retriever = retriever
        self._reranker = reranker
        self._llm = llm
        self.tenant_id = tenant_id or settings.TENANT_ID
        self._input_guard = InputGuardrail(max_length=2000)
        self._output_guard = OutputGuardrail()
        self._doc_sanitizer = DocumentSanitizer()
        self._grounding = GroundingChecker(threshold=settings.AGENT_GROUNDING_THRESHOLD)
        self._workflow = WorkflowOrchestrator(tenant_id=self.tenant_id)
        self._response_builder = ResponseBuilder(
            llm=self._llm, reranker=self._reranker,
            grounding=self._grounding, output_validator=self._output_guard,
            doc_sanitizer=self._doc_sanitizer, workflow=self._workflow,
            session_store=session_store)
        self._intent_router = IntentRouter()
        self._action_proposer = ActionProposer(
            response_builder=self._response_builder,
            session_store=session_store, workflow=self._workflow,
            hris_conn=hris_conn)

    def _ensure_stack(self):
        if self._llm is not None and self._retriever is not None:
            return
        from ..pipeline_query import build_stack
        embedder, store, retriever, reranker, llm = build_stack()
        try:
            if getattr(retriever, "tenant_id", None) != self.tenant_id:
                retriever.tenant_id = self.tenant_id
        except Exception:
            pass
        self._embedder = self._embedder or embedder
        self._store = self._store or store
        self._retriever = self._retriever or retriever
        self._reranker = self._reranker or reranker
        self._llm = self._llm or llm

    def chat(self, question: str, session_id: str = "default", employee_id: str | None = None,
             top_k_final: int | None = None, tenant_id: str | None = None,
             gen: dict | None = None, requester_email: str | None = None) -> dict:
        self._gen = dict(gen or {})
        self._requester_email = requester_email or ""
        self._response_builder._gen = self._gen
        self._response_builder._requester_email = self._requester_email
        self._action_proposer._requester_email = self._requester_email
        try:
            return self._chat_inner(question, session_id, employee_id, top_k_final, tenant_id)
        except Exception as e:
            model = AgentResponseModel(
                status="error",
                answer="Đã xảy ra lỗi khi xử lý yêu cầu. Vui lòng thử lại.",
                evidence=EvidenceSummary(reason=f"error:{type(e).__name__}"),
            )
            return self._response_builder.respond(model, has_evidence=False, flags=[f"error:{e}"],
                                  plan={"retrieve": False, "tool": None, "reason": "exception"},
                                  rewritten_query="", tenant_id=self.tenant_id)

    def _chat_inner(self, question: str, session_id: str = "default", employee_id: str | None = None,
                    top_k_final: int | None = None, tenant_id: str | None = None) -> dict:
        self._ensure_stack()
        if tenant_id:
            self.tenant_id = tenant_id
            try:
                self._retriever.tenant_id = tenant_id
            except Exception:
                pass
        employee_id = employee_id or settings.DEFAULT_EMPLOYEE_ID

        sanitized, flags = self._input_guard.check(question)
        question = sanitized

        rewritten_preview = session_store.rewrite_query(session_id, question, tenant_id=self.tenant_id)

        # WS6: ltm_learn moved out of the hot path — cross-session learning is
        # opt-in via LTM_LEARN_ON_CHAT (default False) instead of an implicit
        # side-effect on every chat() call.  Explicit memory writes still go
        # through POST /memory/store.
        if settings.LTM_ENABLED and settings.LTM_LEARN_ON_CHAT:
            try:
                from .memory import ltm_learn
                ltm_learn(employee_id, self.tenant_id, question)
            except Exception:
                pass

        intent = detect_intent(question, self._llm)
        slots = slots_for_intent(question, intent)
        history_slots = session_store.get_slots(session_id, tenant_id=self.tenant_id)
        for k, v in history_slots.items():
            if k not in slots:
                slots[k] = v

        plan = self._intent_router.decide(intent, question, slots)

        try:
            from ..stream.metrics import registry
            registry.inc("maia_conversations_total")
            registry.inc(f"maia_intent_total_{intent}")
        except Exception:
            pass

        if intent == "leave_request" and "days" not in slots:
            q = "Bạn muốn xin nghỉ bao nhiêu ngày và từ ngày nào? (Ví dụ: 5 ngày từ 10/09)"
            session_store.append(session_id, "user", question, intent, tenant_id=self.tenant_id)
            model = AgentResponseModel(
                status="needs_clarification", answer=q, intent=intent, slots=slots,
                needs_clarification=True, clarification_question=q)
            return self._response_builder.respond(model, has_evidence=False, flags=flags, plan=plan,
                                  rewritten_query=rewritten_preview, tenant_id=self.tenant_id)
        if intent == "leave_request" and "start_date" not in slots:
            q2 = f"Bạn muốn nghỉ {slots['days']} ngày từ ngày nào? Vui lòng cho biết ngày bắt đầu (VD: 10/09)."
            session_store.append(session_id, "user", question, intent, tenant_id=self.tenant_id)
            model = AgentResponseModel(
                status="needs_clarification", answer=q2, intent=intent, slots=slots,
                needs_clarification=True, clarification_question=q2)
            return self._response_builder.respond(model, has_evidence=False, flags=flags, plan=plan,
                                  rewritten_query=rewritten_preview, tenant_id=self.tenant_id)

        if intent == "leave_balance":
            bal = hris_conn.check_leave_balance(employee_id, tenant_id=self.tenant_id)
            try:
                from ..stream.metrics import registry as _r
                _r.inc("maia_tool_calls_total")
            except Exception:
                pass
            iter_res = (self._iterative_retrieve(question, session_id, top_k_final, intent)
                        if plan["retrieve"] else {"context": "", "used": [], "attempts": 0,
                                                  "report": None, "rewritten_queries": []})
            used = iter_res["used"]
            citations = ResponseBuilder.cards(used)
            answer = (f"Bạn còn {bal['balance']} ngày phép năm (nhân viên {employee_id}, "
                      f"nguồn: {bal.get('source', 'mock')}).")
            if used:
                answer += f" Nguồn: {used[0]['metadata'].get('filename', 'Leave Policy')} {used[0].get('cite_tag', '[S1]')}"
            session_store.append(session_id, "user", question, intent, tenant_id=self.tenant_id)
            session_store.append(session_id, "assistant", answer, intent, tenant_id=self.tenant_id)
            model = AgentResponseModel(
                status="answered", answer=answer, intent=intent,
                citations=[Citation(**c) for c in citations],
                evidence=EvidenceSummary(**ResponseBuilder.ev_summary(iter_res)),
                grounding=GroundingInfo(supported=True, score=1.0, cites_valid=True),
                action=ActionResult(type="check_leave_balance", result=bal),
                slots=slots)
            return self._response_builder.respond(model, has_evidence=True, flags=flags, plan=plan,
                                  rewritten_query=rewritten_preview, tenant_id=self.tenant_id)

        iter_res = (self._iterative_retrieve(question, session_id, top_k_final, intent,
                                             corrective=True)
                    if plan["retrieve"] else {"context": "", "used": [], "attempts": 0,
                                              "report": None, "rewritten_queries": [],
                                              "final_query": question})
        context = iter_res["context"]
        used = iter_res["used"]
        report = iter_res["report"]

        if not used or (report is not None and not report.enough):
            return self._response_builder.insufficient(intent, slots, flags, plan, rewritten_preview,
                                          iter_res, self.tenant_id, session_id, question)

        if plan.get("tool") in ("create_it_ticket", "create_leave_request"):
            return self._action_proposer.propose_action(
                question, session_id, employee_id, intent, slots,
                plan, flags, rewritten_preview, iter_res,
                context, used, tenant_id=self.tenant_id)

        answer, cites_valid, score = self._response_builder.generate_grounded(
            question, context, used, session_id,
            employee_id=employee_id, tenant_id=self.tenant_id)
        if answer == INSUFFICIENT_TEXT:
            return self._response_builder.insufficient(intent, slots, flags, plan, rewritten_preview,
                                          iter_res, self.tenant_id, session_id, question)
        valid_out, issues, answer = self._output_guard.check(answer)
        if not valid_out:
            answer += f"\n\n[Guardrail: {'/'.join(issues)}]"
        citations = ResponseBuilder.cards(used)
        session_store.append(session_id, "user", question, intent, tenant_id=self.tenant_id)
        session_store.append(session_id, "assistant", answer, intent, tenant_id=self.tenant_id)
        model = AgentResponseModel(
            status="answered", answer=answer, intent=intent,
            citations=[Citation(**c) for c in citations],
            evidence=EvidenceSummary(**ResponseBuilder.ev_summary(iter_res)),
            grounding=GroundingInfo(supported=True, score=round(score, 4),
                                    cites_valid=cites_valid),
            slots=slots)
        return self._response_builder.respond(model, has_evidence=True, flags=flags, plan=plan,
                             rewritten_query=rewritten_preview, tenant_id=self.tenant_id,
                             iter_res=iter_res)

    def _iterative_retrieve(self, question: str, session_id: str, top_k_final: int | None,
                            intent: str = "general", corrective: bool = False) -> dict:
        from .agentic import AgenticRetriever
        ar = AgenticRetriever(self._retriever, self._reranker, session_store)
        if corrective and settings.CRAG_ENABLED:
            return ar.corrective_retrieve(
                question, session_id=session_id, tenant_id=self.tenant_id,
                top_k_final=top_k_final, llm=self._llm)
        return ar.iterative_retrieve(
            question, session_id=session_id, tenant_id=self.tenant_id,
            top_k_final=top_k_final, expansions=_INTENT_HINTS.get(intent, []))

    _TOOL_RESULT_SHAPES = {
        "check_leave_balance": {"required": ("employee_id", "balance", "unit")},
        "create_leave_request": {"ok_true": ("request_id", "days"), "ok_false": ("error",)},
        "create_it_ticket": {"ok_true": ("ticket_id", "type"), "ok_false": ("error",)},
    }

    @classmethod
    def _validate_tool_result(cls, tool_name: str, result: dict | None, emp: str) -> dict:
        if not isinstance(result, dict) or not result:
            return {"ok": False, "error": f"malformed_tool_result:{tool_name}", "employee_id": emp}
        shape = cls._TOOL_RESULT_SHAPES.get(tool_name)
        if not shape:
            return {"ok": False, "error": f"unknown_tool:{tool_name}", "employee_id": emp}
        ok = bool(result.get("ok"))
        required = shape["ok_true"] if ok else shape["ok_false"]
        missing = [f for f in required if f not in result]
        if missing:
            return {"ok": False, "error": f"malformed_result:{','.join(missing)}",
                    "employee_id": emp}
        return result

    def confirm_action(self, session_id: str, employee_id: str | None = None,
                       approved: bool = True, idempotency_key: str | None = None,
                       is_admin: bool = False) -> dict:
        self._ensure_stack()
        
        if idempotency_key:
            result = self._workflow.check_idempotency(
                idempotency_key=idempotency_key,
                session_id=session_id,
                tenant_id=self.tenant_id)
            if result:
                model = AgentResponseModel(
                    status=result["status"],
                    answer=result["answer"],
                    intent=result["intent"],
                    citations=[Citation(**c) for c in result["citations"]],
                    evidence=EvidenceSummary(**result["evidence"]) if result["evidence"] else EvidenceSummary(),
                    grounding=GroundingInfo(**result["grounding"]),
                    action=result["action"],
                    slots=result["slots"],
                )
                return self._response_builder.respond(model, has_evidence=True, flags=result["flags"],
                                      plan=result["plan"],
                                      rewritten_query=result["rewritten_query"],
                                      tenant_id=self.tenant_id)
        
        pending = session_store.pop_pending(session_id, tenant_id=self.tenant_id)
        if not pending:
            model = AgentResponseModel(
                status="error",
                answer="Không có yêu cầu nào đang chờ duyệt trong phiên này.")
            return self._response_builder.respond(model, has_evidence=False, flags=["no_pending_action"],
                                  plan={"retrieve": False, "tool": None,
                                        "reason": "confirm without pending"},
                                  rewritten_query="", tenant_id=self.tenant_id)
        tool = pending["tool"]
        emp = employee_id or pending.get("employee_id") or settings.DEFAULT_EMPLOYEE_ID
        citations = [Citation(**c) for c in pending.get("citations", [])]
        used_keys = pending.get("used_keys", [])
        intent = pending.get("intent", "general")

        if not approved:
            answer = f"Đã hủy: {pending.get('summary', tool)}. Không có gì được thực hiện."
            session_store.append(session_id, "assistant", answer, intent, tenant_id=self.tenant_id)
            try:
                requester = pending.get("requester_email", "") or emp
                self._workflow.cancel_proposal(
                    tool=tool, session_id=session_id,
                    summary=pending.get("summary", tool),
                    requester=requester, employee_id=emp)
            except Exception:
                pass
            model = AgentResponseModel(
                status="action_cancelled", answer=answer, intent=intent,
                citations=citations,
                evidence=EvidenceSummary(**pending.get("evidence", {})),
                grounding=GroundingInfo(supported=True, score=1.0, cites_valid=True),
                action=ActionResult(type=tool, status="cancelled",
                                    result={"ok": False, "cancelled": True}),
                slots=pending.get("slots", {}))
            return self._response_builder.respond(model, has_evidence=True, flags=[],
                                  plan={"retrieve": False, "tool": tool,
                                        "reason": "cancelled by user"},
                                  rewritten_query="", tenant_id=self.tenant_id)

        try:
            from ..stream.metrics import registry as _r
            _r.inc("maia_tool_calls_total")
        except Exception:
            pass
        pending_tenant = pending.get("tenant_id")
        if pending_tenant is not None and pending_tenant != self.tenant_id:
            model = AgentResponseModel(
                status="error",
                answer="Phiên làm việc của bạn không khớp với yêu cầu đang chờ duyệt.")
            return self._response_builder.respond(model, has_evidence=False,
                                  flags=["tenant_mismatch_on_confirm"],
                                  plan={"retrieve": False, "tool": tool,
                                        "reason": "tenant boundary violation"},
                                  rewritten_query="", tenant_id=self.tenant_id)
        pending_emp = pending.get("employee_id")
        if not is_admin and pending_emp and employee_id and pending_emp != employee_id:
            model = AgentResponseModel(
                status="error",
                answer="Bạn không thể xác nhận hành động của nhân viên khác.")
            return self._response_builder.respond(model, has_evidence=False,
                                  flags=["cross_employee_confirm_blocked"],
                                  plan={"retrieve": False, "tool": tool,
                                        "reason": "employee ownership violation"},
                                  rewritten_query="", tenant_id=self.tenant_id)
        verify = None
        if tool == "create_it_ticket":
            result = hris_conn.create_it_ticket(
                emp, pending["params"].get("ticket_type", "general"),
                pending["params"].get("description", ""), tenant_id=self.tenant_id)
            result = self._validate_tool_result(tool, result, emp)
            if result.get("ok"):
                try:
                    verify = hris_conn.verify_ticket(result["ticket_id"])
                except Exception:
                    verify = None
                answer = (f"Đã tạo ticket {result['ticket_id']} ({result['type']}) — "
                          f"{result['assignee']}"
                          + (f" (verified: {verify.get('status')})." if verify else "."))
            else:
                answer = f"Không thể tạo ticket: {result.get('error', 'lỗi không rõ')}."
        elif tool == "create_leave_request":
            result = hris_conn.create_leave_request(
                emp, days=pending["params"].get("days", 1),
                start_date=pending["params"].get("start_date"),
                tenant_id=self.tenant_id)
            result = self._validate_tool_result(tool, result, emp)
            if result.get("ok"):
                try:
                    verify = (hris_conn.verify_ticket(result["request_id"])
                              if hasattr(hris_conn, "verify_ticket") else None)
                except Exception:
                    verify = None
                answer = (f"Đã tạo yêu cầu nghỉ phép {result['days']} ngày từ "
                          f"{result['start_date']}. Mã: {result['request_id']}. "
                          f"Trạng thái: {result['status']}. Còn lại: "
                          f"{result.get('remaining_balance', '?')} ngày.")
            else:
                answer = result.get("error", "Không thể tạo yêu cầu.")
        else:
            result = {"ok": False, "error": f"Unknown tool: {tool}"}
            answer = result["error"]

        ok = bool(result.get("ok"))
        if ok and citations:
            answer += f" Căn cứ: {citations[0].tag} {citations[0].filename}."
        valid_out, issues, answer = self._output_guard.check(
            answer, is_action_response=True)
        if not valid_out:
            answer += f"\n\n[Guardrail: {'/'.join(issues)}]"
        try:
            requester = pending.get("requester_email", "") or emp
            ref = (result.get("ticket_id") or result.get("request_id") or "")
            self._workflow.confirm_proposal(
                tool=tool, session_id=session_id, result=result,
                requester=requester, employee_id=emp, ref=ref, approved=ok,
                summary=pending.get("summary", tool))
        except Exception:
            pass
        cc = CitationChecker([{"chunk_id": k["chunk_id"]} for k in used_keys])
        cites_valid, _ = cc.check(answer)
        session_store.append(session_id, "assistant", answer, intent, tenant_id=self.tenant_id)
        model = AgentResponseModel(
            status="action_completed", answer=answer, intent=intent,
            citations=citations,
            evidence=EvidenceSummary(**pending.get("evidence", {})),
            grounding=GroundingInfo(supported=ok, score=1.0 if ok else 0.0,
                                    cites_valid=cites_valid),
            action=ActionResult(type=tool, status="completed" if ok else "failed",
                                result=result, verify=verify),
            slots=pending.get("slots", {}))
        return self._response_builder.respond(model, has_evidence=True, flags=[],
                             plan={"retrieve": False, "tool": tool,
                                   "reason": "approved and executed"},
                             rewritten_query="", tenant_id=self.tenant_id)

    def stream_answer(self, question: str, session_id: str = "default", employee_id: str | None = None, tenant_id: str | None = None, requester_email: str | None = None):
        result = self.chat(question, session_id, employee_id, tenant_id=tenant_id, requester_email=requester_email)
        answer = result["answer"]
        import re
        parts = re.split(r"(?<=[.!?])\s+", answer)
        for p in parts:
            if p:
                yield p + " "
