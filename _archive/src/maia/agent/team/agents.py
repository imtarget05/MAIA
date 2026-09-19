"""Concrete team members built on EnterpriseAgent (no new framework).

- RouterAgent: rule-based intent classification -> hr | it | knowledge.
- KnowledgeAgent: full-power grounded Q&A (any intent).
- HRAgent / ITAgent: scoped to their intent sets + tools. Out-of-scope
  questions return an out_of_scope result so the orchestrator reroutes.
- Approval gate is NEVER bypassed: members only ever return EnterpriseAgent
  responses (side effects stay proposed in pending_action); confirm_action
  is only ever called by the human via API/CLI.
"""
from __future__ import annotations

from typing import Any

from ..intents import Intent, detect_intent
from ..tools import TOOL_REGISTRY
from .base import AgentMessage, AgentRole, TeamAgent, TeamBus

HR_INTENTS = frozenset({"leave_request", "leave_balance", "hr_policy",
                        "benefits", "expense", "onboarding"})
IT_INTENTS = frozenset({"it_help", "vpn", "security"})

HR_TOOLS = ("check_leave_balance", "create_leave_request")
IT_TOOLS = ("create_it_ticket",)


def route_for_intent(intent: str) -> str:
    """Intent -> member name. Unknown/general questions go to knowledge."""
    if intent in HR_INTENTS:
        return "hr"
    if intent in IT_INTENTS:
        return "it"
    return "knowledge"


class RouterAgent(TeamAgent):
    role = AgentRole(name="router", description="Receptionist: classifies and routes.",
                     tools=(), can_delegate=True)

    def handle(self, msg: AgentMessage) -> AgentMessage:
        try:
            question = (msg.payload.get("question") or "").strip()
            if not question:
                return self._reply(msg, "result",
                                   {"route": "knowledge", "intent": "general", "note": "empty question"})
            intent: Intent = detect_intent(question)
            return self._reply(msg, "result",
                               {"route": route_for_intent(intent), "intent": intent})
        except Exception as e:
            return self._fail(msg, f"{type(e).__name__}: {e}")


class _MemberBase(TeamAgent):
    """Scoped EnterpriseAgent wrapper. Lazy agent: injectable for tests."""

    scope: frozenset = frozenset()
    allow_general: bool = True

    def __init__(self, bus: TeamBus | None = None, agent: Any | None = None):
        super().__init__(bus)
        self._agent = agent

    def _agent_or_default(self) -> Any:
        if self._agent is None:
            from ..agent import EnterpriseAgent  # deferred: avoids import cycle
            self._agent = EnterpriseAgent()
        return self._agent

    def _task_args(self, msg: AgentMessage) -> dict:
        p = msg.payload
        return {"question": p.get("question", ""),
                "session_id": p.get("session_id", "team_default"),
                "employee_id": p.get("employee_id"),
                "top_k_final": p.get("top_k"),
                "tenant_id": p.get("tenant_id")}

    def handle(self, msg: AgentMessage) -> AgentMessage:
        try:
            question = (msg.payload.get("question") or "").strip()
            if not question:
                return self._reply(msg, "result", {"status": "error", "member": self.role.name,
                                                   "answer": "Câu hỏi trống."})
            intent = detect_intent(question)
            if self.scope and intent not in self.scope and not (
                    self.allow_general and intent == "general"):
                return self._reply(msg, "result",
                                   {"status": "out_of_scope", "member": self.role.name,
                                    "intent": intent,
                                    "answer": "",
                                    "note": f"{self.role.name} handles {sorted(self.scope)}"})
            res = dict(self._agent_or_default().chat(**self._task_args(msg)))
            res["member"] = self.role.name
            return self._reply(msg, "result", res)
        except Exception as e:
            return self._fail(msg, f"{type(e).__name__}: {e}")


class KnowledgeAgent(_MemberBase):
    role = AgentRole(name="knowledge", description="Full grounded Q&A over all knowledge.",
                     tools=tuple(TOOL_REGISTRY))
    scope = frozenset()  # empty scope = accepts everything


class HRAgent(_MemberBase):
    role = AgentRole(name="hr", description="HR scoped: leave, policy, benefits, expense, onboarding.",
                     tools=HR_TOOLS)
    scope = HR_INTENTS


class ITAgent(_MemberBase):
    role = AgentRole(name="it", description="IT scoped: devices, VPN, security incidents.",
                     tools=IT_TOOLS)
    scope = IT_INTENTS


def default_members(bus: TeamBus | None = None) -> dict[str, TeamAgent]:
    """Production member set (agents lazy-load the real stack on first use)."""
    return {"router": RouterAgent(bus),
            "knowledge": KnowledgeAgent(bus),
            "hr": HRAgent(bus),
            "it": ITAgent(bus)}
