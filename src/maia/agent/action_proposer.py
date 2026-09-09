"""ActionProposer — builds grounded answer + pending_action card for
side-effect tools (C1: confirm-before-action).

Extracted from EnterpriseAgent._propose_action.
"""
from __future__ import annotations

from .intents import ticket_type_for
from .response_builder import ResponseBuilder
from .schemas import (
    INSUFFICIENT_TEXT,
    AgentResponseModel,
    Citation,
    EvidenceSummary,
    GroundingInfo,
    PendingAction,
)


class ActionProposer:
    """Handles proposal of side-effect actions with grounded explanation
    and pending action storage.
    """

    def __init__(self, response_builder: ResponseBuilder,
                 session_store, workflow, hris_conn,
                 requester_email: str = ""):
        self._rb = response_builder
        self._session_store = session_store
        self._workflow = workflow
        self._hris_conn = hris_conn
        self._requester_email = requester_email

    def propose_action(self, question: str, session_id: str, employee_id: str,
                       intent: str, slots: dict, plan: dict, flags: list,
                       rewritten_preview: str, iter_res: dict,
                       context: str, used: list[dict],
                       tenant_id: str) -> dict:
        """Build a grounded answer + pending_action card; store packet for confirm."""
        tool = plan["tool"]
        if tool == "create_it_ticket":
            ttype = plan.get("ticket_type") or ticket_type_for(question, intent)
            params = {"ticket_type": ttype, "description": question}
            summary = (f"Ticket {ttype} — IT Help Desk ext 202 "
                       f"(nhân viên {employee_id})")
        else:  # create_leave_request
            try:
                preview_bal = self._hris_conn.check_leave_balance(
                    employee_id, tenant_id=tenant_id).get("balance", "?")
            except Exception:
                preview_bal = "?"
            params = {"days": slots["days"], "start_date": slots["start_date"]}
            summary = (f"Nghỉ phép {slots['days']} ngày từ {slots['start_date']} "
                       f"(còn {preview_bal} ngày, nhân viên {employee_id})")
        # Grounded explanation of what will happen (validated like any answer).
        answer, cites_valid, score = self._rb.generate_grounded(
            question, context, used, session_id,
            employee_id=employee_id, tenant_id=tenant_id)
        if answer == INSUFFICIENT_TEXT:
            return self._rb.insufficient(intent, slots, flags, plan, rewritten_preview,
                                          iter_res, tenant_id, session_id, question)
        answer += (f"\n\nĐề xuất hành động (chưa thực hiện): {summary}.\n"
                   f"Vui lòng xác nhận để tôi tiếp tục.")
        citations = self._rb.cards(used)
        self._session_store.set_pending(session_id, {
            "tool": tool, "params": params, "summary": summary,
            "question": question, "intent": intent, "slots": slots,
            "employee_id": employee_id, "tenant_id": tenant_id,
            "requester_email": self._requester_email or "",
            "citations": citations,
            "used_keys": [{"chunk_id": c.get("chunk_id", ""),
                           "cite_tag": c.get("cite_tag", f"[S{i + 1}]")}
                          for i, c in enumerate(used)],
            "evidence": self._rb.ev_summary(iter_res),
        }, tenant_id=tenant_id)
        self._session_store.append(session_id, "user", question, intent, tenant_id=tenant_id)
        self._session_store.append(session_id, "assistant", answer, intent, tenant_id=tenant_id)
        # Workflow mirror (admin dashboard) + department notification (best-effort).
        try:
            requester = self._requester_email or employee_id or ""
            used_keys = [{"chunk_id": c.get("chunk_id", ""),
                           "cite_tag": c.get("cite_tag", f"[S{i + 1}]")}
                          for i, c in enumerate(used)]
            self._workflow.record_proposal(
                tool=tool, session_id=session_id, employee_id=employee_id or "",
                params=params, summary=summary, question=question,
                intent=intent, slots=slots, citations=citations,
                used_keys=used_keys, evidence=self._rb.ev_summary(iter_res),
                requester_email=requester)
        except Exception:
            pass
        model = AgentResponseModel(
            status="needs_approval", answer=answer, intent=intent,
            citations=[Citation(**c) for c in citations],
            evidence=EvidenceSummary(**self._rb.ev_summary(iter_res)),
            grounding=GroundingInfo(supported=True, score=round(score, 4),
                                    cites_valid=cites_valid),
            pending_action=PendingAction(type=tool, params=params, summary=summary),
            slots=slots)
        return self._rb.respond(model, has_evidence=True, flags=flags, plan=plan,
                                 rewritten_query=rewritten_preview,
                                 tenant_id=tenant_id,
                                 iter_res=iter_res)
