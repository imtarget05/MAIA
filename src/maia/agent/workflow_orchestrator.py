"""Workflow orchestration: proposal recording, workflow mirroring,
and department notification.

Extracted from EnterpriseAgent._propose_action (agent/agent.py)
to separate workflow/notification concerns from agent logic.
"""
import time
import uuid

from .. import notifier as _nt
from .. import workflow as _wf
from ..config import settings
from .session import session_store


class WorkflowOrchestrator:
    """Handles workflow recording, idempotency keys, and notifications
    for proposed side-effect actions (C1: confirm-before-action).
    """

    def __init__(self, tenant_id: str = settings.TENANT_ID):
        self.tenant_id = tenant_id

    def record_proposal(
        self,
        tool: str,
        session_id: str,
        employee_id: str,
        params: dict,
        summary: str,
        question: str,
        intent: str,
        slots: dict,
        citations: list[dict],
        used_keys: list[dict],
        evidence: dict,
        requester_email: str = "",
    ) -> str:
        """Record a pending action proposal with workflow mirror and notification.

        Returns the idempotency key for the proposal.
        """
        requester = requester_email or employee_id or ""
        idempotency_key = f"{tool}:{session_id}:{int(time.time()*1000)}:{uuid.uuid4().hex[:8]}"

        _wf.record_proposal(
            type=tool, session_id=session_id, tool=tool,
            requester_email=requester, employee_id=employee_id or "",
            tenant_id=self.tenant_id, summary=summary, params=params,
            idempotency_key=idempotency_key,
        )

        # Store idempotency key in pending for later confirmation
        pending = session_store.get_pending(session_id, tenant_id=self.tenant_id)
        if pending:
            pending["idempotency_key"] = idempotency_key
            session_store.set_pending(session_id, pending, tenant_id=self.tenant_id)

        _nt.notify_new_request(tool, summary, requester or employee_id or "")
        return idempotency_key

    def cancel_proposal(
        self,
        tool: str,
        session_id: str,
        summary: str,
        requester: str,
        employee_id: str,
    ) -> None:
        """Cancel a pending proposal with workflow and notification."""
        _wf.decide_by_session(session_id, tool, False, decided_by=employee_id)
        _nt.notify_request_decided(tool, summary, requester, False, employee_id)

    def confirm_proposal(
        self,
        tool: str,
        session_id: str,
        result: dict,
        requester: str,
        employee_id: str,
        ref: str,
        approved: bool,
    ) -> None:
        """Confirm or reject a proposal after approval."""
        if approved:
            _wf.decide_by_session(session_id, tool, True, decided_by=employee_id,
                                  result_ref=ref, result=result)
        else:
            _wf.decide_by_session(session_id, tool, False, decided_by=employee_id,
                                  result_ref=ref, result=result)
        _nt.notify_request_decided(tool, summary if hasattr(self, 'summary') else '',
                                   requester, approved, employee_id, ref)

    def check_idempotency(
        self,
        idempotency_key: str,
        session_id: str,
        tenant_id: str,
    ) -> dict | None:
        """Check if an idempotent replay exists for the given key.

        Returns the existing result dict if found, None otherwise.
        """
        from .. import workflow as _wf
        con = _wf._connect()
        try:
            cur = con.execute(
                "SELECT * FROM requests WHERE idempotency_key=? AND status IN ('approved', 'rejected')",
                (idempotency_key,),
            )
            hit = cur.fetchone()
            if hit:
                row = _wf._row_to_dict(hit)
                pending = session_store.get_pending(session_id, tenant_id=tenant_id)
                return {
                    "status": "action_completed" if row["status"] == "approved" else "action_cancelled",
                    "answer": f"Đã xử lý trước đó (idempotency_key={idempotency_key}): {row.get('result', {})}",
                    "intent": pending.get("intent", "general") if pending else "general",
                    "citations": [c for c in (pending.get("citations", []) if pending else [])],
                    "evidence": pending.get("evidence", {}) if pending else {},
                    "grounding": {"supported": True, "score": 1.0, "cites_valid": True},
                    "action": {"type": row["tool"], "status": row["status"], "result": row.get("result", {})},
                    "slots": pending.get("slots", {}) if pending else {},
                    "flags": ["idempotent_replay"],
                    "plan": {"retrieve": False, "tool": row["tool"], "reason": "idempotent replay"},
                    "rewritten_query": "",
                    "tenant_id": tenant_id,
                }
        finally:
            con.close()
        return None
