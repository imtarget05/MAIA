"""IntentRouter — determines what action to take based on intent and slots.

Extracted from EnterpriseAgent._decide.
"""
from __future__ import annotations

from typing import Any

from .intents import ticket_type_for, wants_action
from .tools import TOOL_REGISTRY


class IntentRouter:
    """Knowledge Agent Decide step (§16): choose Retrieve / Memory / Tool.

    Side-effect tools set requires_approval=True: chat() only PROPOSES,
    confirm_action() executes (C1).
    """

    def decide(self, intent: str, question: str, slots: dict) -> dict[str, Any]:
        plan: dict[str, Any] = {
            "retrieve": True, "tool": None, "requires_approval": False,
            "ticket_type": None, "memory": True, "reason": "",
        }
        ql = (question or "").lower()
        if intent == "leave_balance":
            plan["tool"] = "check_leave_balance"
            plan["reason"] = "Balance check requires DB tool + retrieve policy for citation"
        elif intent == "leave_request":
            if "days" in slots and "start_date" in slots:
                plan["tool"] = "create_leave_request"
                plan["requires_approval"] = True
                plan["reason"] = "All slots present → propose request + retrieve policy"
            else:
                plan["retrieve"] = False
                plan["reason"] = "Missing slots → clarification, no retrieval yet"
        elif intent in ("vpn", "it_help", "security"):
            ttype = ticket_type_for(question, intent)
            plan["ticket_type"] = ttype
            is_howto = (ql.strip().startswith("cách") or "cách " in ql[:20]
                        or "như thế nào" in ql or "làm gì" in ql
                        or "liên hệ" in ql or "bộ phận nào" in ql)
            propose = False
            if intent == "security" or wants_action(question):
                propose = True
            if propose:
                plan["tool"] = "create_it_ticket"
                plan["requires_approval"] = True
                plan["reason"] = f"Intent {intent} → propose {ttype} ticket for approval"
            else:
                plan["reason"] = (f"Intent {intent} how-to question → retrieve procedure only"
                                  + ("" if is_howto else " (no explicit action request)"))
        else:
            plan["reason"] = "General knowledge question → RAG retrieve"
        # Tool allowlist enforcement: only propose tools present in TOOL_REGISTRY
        if plan.get("tool") and plan["tool"] not in TOOL_REGISTRY:
            plan["tool"] = None
            plan["requires_approval"] = False
            plan["reason"] += " | tool not in allowlist"
        return plan
