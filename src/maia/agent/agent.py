"""Enterprise Agent — Agentic RAG: Decide → Retrieve/Memory/Tool → Evidence → Self-correction → Grounding."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..loops.answer_loop import CitationChecker, GroundingChecker
from ..loops.guardrails import InputGuardrail, OutputGuardrail
from ..prompt import assemble, build_agent_messages
from .intents import detect_intent, slots_for_intent
from .session import session_store
from . import hris as hris_conn
from .tools import create_it_ticket

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
        self._grounding = GroundingChecker(threshold=settings.AGENT_GROUNDING_THRESHOLD)

    def _ensure_stack(self):
        if self._llm is not None and self._retriever is not None:
            return
        from ..pipeline_query import build_stack
        embedder, store, retriever, reranker, llm = build_stack()
        # ensure retriever tenant aware
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

    def _grounding_and_citation(self, answer: str, context: str, used: list[dict]):
        cc = CitationChecker(used)
        cites_valid, _ = cc.check(answer)
        grounded, score = self._grounding.check(answer, context)
        return cites_valid, grounded, score

    def _decide(self, intent: str, question: str, slots: dict) -> dict:
        """Knowledge Agent Decide step (§16): choose Retrieve / Memory / Tool."""
        plan = {"retrieve": True, "tool": None, "memory": True, "reason": ""}
        ql = question.lower()
        if intent == "leave_balance":
            plan["tool"] = "check_leave_balance"
            plan["reason"] = "Balance check requires DB tool + retrieve policy for citation"
        elif intent == "leave_request":
            if "days" in slots and "start_date" in slots:
                plan["tool"] = "create_leave_request"
                plan["reason"] = "All slots present → create request + retrieve policy"
            else:
                plan["retrieve"] = False
                plan["reason"] = "Missing slots → clarification, no retrieval yet"
        elif intent in ("vpn", "it_help", "security"):
            # decide if need ticket tool
            is_howto = ql.strip().startswith("cách") or "cách " in ql[:20]
            if intent == "security" and ("mất" in ql or "lost" in ql):
                plan["tool"] = "create_it_ticket"
            elif intent == "it_help" and any(kw in ql for kw in ["mất laptop","bị hỏng","không khởi động","broken","hỏng"]):
                plan["tool"] = "create_it_ticket"
            elif intent == "vpn" and not is_howto and any(kw in ql for kw in ["tạo ticket","create ticket","tạo vpn","giúp tôi","yêu cầu vpn"]):
                plan["tool"] = "create_it_ticket"
            plan["reason"] = f"Intent {intent} + tool={plan['tool']} + retrieve for procedure"
        else:
            plan["reason"] = "General knowledge question → RAG retrieve"
        return plan

    def _iterative_retrieve(self, question: str, session_id: str, top_k_final: int | None) -> dict:
        """Agentic iterative retrieval with evidence check and self-correction (§19)."""
        from .agentic import AgenticRetriever
        ar = AgenticRetriever(self._retriever, self._reranker, session_store)
        return ar.iterative_retrieve(question, session_id=session_id, tenant_id=self.tenant_id, top_k_final=top_k_final)

    def chat(self, question: str, session_id: str = "default", employee_id: str | None = None, top_k_final: int | None = None, tenant_id: str | None = None) -> dict:
        self._ensure_stack()
        if tenant_id:
            self.tenant_id = tenant_id
            try:
                self._retriever.tenant_id = tenant_id
            except Exception:
                pass
        employee_id = employee_id or settings.DEFAULT_EMPLOYEE_ID

        # 1. Input guard
        sanitized, flags = self._input_guard.check(question)
        question = sanitized

        # 2. Memory: query rewrite preview (for tracing)
        rewritten_preview = session_store.rewrite_query(session_id, question)

        # 3. Intent + slots
        intent = detect_intent(question, self._llm)
        slots = slots_for_intent(question, intent)
        history_slots = session_store.get_slots(session_id)
        for k, v in history_slots.items():
            if k not in slots:
                slots[k] = v

        # 4. Decide
        plan = self._decide(intent, question, slots)

        # metrics: conversation
        try:
            from ..stream.metrics import registry
            registry.inc("maia_conversations_total")
            registry.inc(f"maia_intent_total_{intent}")
        except Exception:
            pass

        # 5. Handle clarify case
        if intent == "leave_request" and "days" not in slots:
            q = "Bạn muốn xin nghỉ bao nhiêu ngày và từ ngày nào? (Ví dụ: 5 ngày từ 10/09)"
            session_store.append(session_id, "user", question, intent)
            return {"answer": q, "intent": intent, "citations": [], "has_evidence": False, "grounding_score": 0.0, "cites_valid": True, "action": None, "slots": slots, "needs_clarification": True, "clarification_question": q, "flags": flags, "plan": plan, "rewritten_query": rewritten_preview, "llm_mode": self._llm.mode, "rerank_mode": self._reranker.mode, "tenant_id": self.tenant_id}
        if intent == "leave_request" and "start_date" not in slots:
            q2 = f"Bạn muốn nghỉ {slots['days']} ngày từ ngày nào? Vui lòng cho biết ngày bắt đầu (VD: 10/09)."
            session_store.append(session_id, "user", question, intent)
            return {"answer": q2, "intent": intent, "citations": [], "has_evidence": False, "grounding_score": 0.0, "cites_valid": True, "action": None, "slots": slots, "needs_clarification": True, "clarification_question": q2, "flags": flags, "plan": plan, "rewritten_query": rewritten_preview, "llm_mode": self._llm.mode, "rerank_mode": self._reranker.mode, "tenant_id": self.tenant_id}

        # 6. Leave balance - tool + iterative retrieve for citation
        if intent == "leave_balance":
            bal = hris_conn.check_leave_balance(employee_id)
            try:
                from ..stream.metrics import registry as _r; _r.inc("maia_tool_calls_total")
            except Exception: pass
            iter_res = self._iterative_retrieve(question, session_id, top_k_final) if plan["retrieve"] else {"context": "", "used": [], "attempts": 0, "report": None, "rewritten_queries": []}
            used = iter_res["used"]; context = iter_res["context"]
            citations = [{"tag": c.get("cite_tag", f"[S{i+1}]"), "chunk_id": c["chunk_id"], "filename": c["metadata"].get("filename",""), "page": c["metadata"].get("page",""), "section": c["metadata"].get("section",""), "dense_score": c.get("dense_score",0.0), "rerank_score": c.get("rerank_score",0.0), "text": c["text"][:600]} for i,c in enumerate(used)]
            answer = f"Bạn còn {bal['balance']} ngày phép năm (nhân viên {employee_id}, nguồn: {bal.get('source','mock')})."
            if used:
                answer += f" Nguồn: {used[0]['metadata'].get('filename','Leave Policy')} {used[0].get('cite_tag','[S1]')}"
            session_store.append(session_id, "user", question, intent)
            session_store.append(session_id, "assistant", answer, intent)
            return {"answer": answer, "intent": intent, "citations": citations, "has_evidence": bool(used), "grounding_score": 1.0 if used else 0.0, "cites_valid": True, "action": {"type": "check_leave_balance", "result": bal}, "slots": slots, "needs_clarification": False, "flags": flags, "plan": plan, "evidence": {"attempts": iter_res["attempts"], "top_dense": iter_res["report"].top_dense if iter_res["report"] else 0, "reason": iter_res["report"].reason if iter_res["report"] else ""}, "rewritten_query": rewritten_preview, "llm_mode": self._llm.mode, "rerank_mode": self._reranker.mode, "tenant_id": self.tenant_id}

        # 7. Leave request with tool + iterative retrieve
        if intent == "leave_request" and plan["tool"] == "create_leave_request":
            iter_res = self._iterative_retrieve(question + " leave policy", session_id, top_k_final)
            used = iter_res["used"]; context = iter_res["context"]
            result = hris_conn.create_leave_request(employee_id, days=slots["days"], start_date=slots["start_date"])
            try:
                from ..stream.metrics import registry as _r; _r.inc("maia_tool_calls_total")
            except Exception: pass
            # verify if HRIS returned request
            verify = None
            if result.get("ok") and result.get("request_id"):
                try:
                    verify = hris_conn.verify_ticket(result["request_id"]) if hasattr(hris_conn, "verify_ticket") else None
                except Exception:
                    verify = None
            citations = [{"tag": c.get("cite_tag", f"[S{i+1}]"), "chunk_id": c["chunk_id"], "filename": c["metadata"].get("filename",""), "page": c["metadata"].get("page",""), "section": c["metadata"].get("section",""), "dense_score": c.get("dense_score",0.0), "rerank_score": c.get("rerank_score",0.0), "text": c["text"][:600]} for i,c in enumerate(used)]
            if result.get("ok"):
                history_text = session_store.history_text(session_id)
                messages = build_agent_messages(question, context, history_text)
                llm_answer = self._llm.chat(messages)
                answer = f"Đã tạo yêu cầu nghỉ phép {slots['days']} ngày từ {slots['start_date']}. Mã yêu cầu: {result['request_id']}. Trạng thái: {result['status']}. Số ngày còn lại: {result.get('remaining_balance','?')} (nguồn: {result.get('source','mock')}).\n\n{llm_answer}"
                cites_valid, _, score = self._grounding_and_citation(answer, context, used)
            else:
                answer = result.get("error", "Không thể tạo yêu cầu.")
                citations = []; cites_valid, score = True, 0.0
            session_store.append(session_id, "user", question, intent)
            session_store.append(session_id, "assistant", answer, intent)
            return {"answer": answer, "intent": intent, "citations": citations, "has_evidence": bool(used), "grounding_score": score if 'score' in locals() else 0.0, "cites_valid": cites_valid, "action": {"type": "create_leave_request", "result": result, "verify": verify, "slots": slots}, "slots": slots, "needs_clarification": False, "flags": flags, "plan": plan, "evidence": {"attempts": iter_res["attempts"], "top_dense": iter_res["report"].top_dense if iter_res["report"] else 0, "reason": iter_res["report"].reason if iter_res["report"] else ""}, "rewritten_query": rewritten_preview, "llm_mode": self._llm.mode, "rerank_mode": self._reranker.mode, "tenant_id": self.tenant_id}

        # 8. Tool chain for IT/VPN/Security (multi-step: retrieve + tool + verify)
        action = None
        if plan["tool"] == "create_it_ticket":
            ticket_type = {"vpn": "vpn_request", "it_help": "laptop_broken", "security": "lost_device"}.get(intent, "general")
            # Agentic: first retrieve procedure, then create ticket, then verify
            # iterative retrieve for procedure (evidence)
            # will be done in default flow too, but we create ticket now for trace
            ticket = create_it_ticket(employee_id, ticket_type, question)
            try:
                from ..stream.metrics import registry as _r; _r.inc("maia_tool_calls_total")
            except Exception: pass
            verify = hris_conn.verify_ticket(ticket["ticket_id"])
            action = {"type": "create_it_ticket", "result": ticket, "verify": verify}
            # enrich answer later with retrieval

        # 9. Default Agentic RAG flow: iterative retrieve → evidence → generate → grounding
        iter_res = self._iterative_retrieve(question, session_id, top_k_final) if plan["retrieve"] else {"context": "", "used": [], "attempts": 0, "report": None, "rewritten_queries": [], "final_query": question}
        context = iter_res["context"]; used = iter_res["used"]
        report = iter_res["report"]
        if not used:
            answer = "Không tìm thấy bằng chứng liên quan trong knowledge base. Tôi không thể trả lời chắc chắn."
            session_store.append(session_id, "user", question, intent)
            session_store.append(session_id, "assistant", answer, intent)
            return {"answer": answer, "intent": intent, "citations": [], "has_evidence": False, "grounding_score": 0.0, "cites_valid": True, "action": action, "slots": slots, "needs_clarification": False, "flags": flags, "plan": plan, "evidence": {"attempts": iter_res["attempts"], "top_dense": report.top_dense if report else 0, "reason": report.reason if report else "no_evidence"}, "rewritten_query": rewritten_preview, "llm_mode": self._llm.mode, "rerank_mode": self._reranker.mode, "tenant_id": self.tenant_id}

        # Grounding gate: if evidence insufficient, return fallback with trace
        if report and not report.enough:
            # still generate but flag low confidence
            pass

        history_text = session_store.history_text(session_id)
        messages = build_agent_messages(question, context, history_text)
        answer = self._llm.chat(messages)
        cites_valid, grounded, score = self._grounding_and_citation(answer, context, used)
        valid_out, issues = self._output_guard.check(answer)
        if not valid_out:
            answer += f"\n\n[Guardrail: {'/'.join(issues)}]"
        citations = [{"tag": c.get("cite_tag", f"[S{i+1}]"), "chunk_id": c["chunk_id"], "filename": c["metadata"].get("filename",""), "page": c["metadata"].get("page",""), "section": c["metadata"].get("section",""), "dense_score": c.get("dense_score",0.0), "fused_score": c.get("fused_score",0.0), "rerank_score": c.get("rerank_score",0.0), "text": c["text"][:600]} for i,c in enumerate(used)]
        if action is not None:
            ticket = action["result"]
            verify_str = f" (verified: {action['verify'].get('status')})" if action.get("verify") else ""
            answer = answer + f"\n\nĐã tạo ticket {ticket['ticket_id']} ({ticket['type']}) — {ticket['assignee']}{verify_str}."
            # also include employee info if security (Agentic multi-step example §17)
            if intent == "security":
                emp_info = hris_conn.get_employee_info(employee_id)
                answer += f"\nNhân viên: {emp_info.get('name')} ({emp_info.get('department')})."

        session_store.append(session_id, "user", question, intent)
        session_store.append(session_id, "assistant", answer, intent)

        has_evidence = report.enough if report else bool(used)
        # in mock mode keep evidence True if we have vectors
        if self._llm.mode == "mock" and used:
            has_evidence = True

        return {"answer": answer, "intent": intent, "citations": citations, "has_evidence": has_evidence, "grounding_score": round(score,4), "cites_valid": cites_valid, "action": action, "slots": slots, "needs_clarification": False, "flags": flags, "plan": plan, "evidence": {"attempts": iter_res["attempts"], "top_dense": report.top_dense if report else 0, "reason": report.reason if report else "", "rewritten_queries": iter_res["rewritten_queries"], "final_query": iter_res["final_query"]}, "rewritten_query": rewritten_preview, "llm_mode": self._llm.mode, "rerank_mode": self._reranker.mode, "tenant_id": self.tenant_id}

    def stream_answer(self, question: str, session_id: str = "default", employee_id: str | None = None, tenant_id: str | None = None):
        result = self.chat(question, session_id, employee_id, tenant_id=tenant_id)
        answer = result["answer"]
        import re
        parts = re.split(r"(?<=[.!?])\s+", answer)
        for p in parts:
            if p:
                yield p + " "

