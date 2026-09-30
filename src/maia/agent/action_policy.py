"""Action risk policy — explicit READ_ONLY / LOW_RISK / HIGH_RISK / FORBIDDEN mapping.

Phase A (additive-only): this module introduces NO new tools and changes NO
production routing, retrieval, or Gate 8 thresholds. It names the risk of
every real tool in ``TOOL_REGISTRY`` plus the destructive admin semantics
MAIA must never execute (no executor exists for them; the policy rejects
them before routing).

Levels
------
READ_ONLY   → may execute without human approval (no side effects).
LOW_RISK    → reserved level; no current MAIA tool maps here. Executable
              under existing application policy when a tool claims it.
HIGH_RISK   → must create pending_action / needs_approval; must NOT
              execute before confirm_action() (C1 confirm-before-action).
FORBIDDEN   → rejected deterministically; must never reach an executor,
              and approval must NOT turn FORBIDDEN into allowed.
UNKNOWN     → fail closed (BLOCK) unless existing documented behaviour
              says otherwise.

Real-tool mapping (must stay in sync with TOOL_REGISTRY in tools.py):

    check_leave_balance   READ_ONLY
    get_employee_requests READ_ONLY
    get_it_tickets        READ_ONLY
    extract_entities      READ_ONLY
    create_leave_request  HIGH_RISK
    create_it_ticket      HIGH_RISK

Policy-only FORBIDDEN actions (no executor in TOOL_REGISTRY — the absence
of an executor is itself part of the guarantee; the policy layer rejects
them before routing is even consulted):

    delete_audit_history   — destroying audit evidence.
    mass_disable_users     — bulk destructive identity action.
    remove_aduser_wildcard — shell-safety fixture equivalent to
                             ``Remove-ADUser *``. MAIA has no PowerShell
                             executor; the real injection case belongs to
                             Phase B (Helpdesk Safe Automation Gateway).
"""
from __future__ import annotations

READ_ONLY = "READ_ONLY"
LOW_RISK = "LOW_RISK"
HIGH_RISK = "HIGH_RISK"
FORBIDDEN = "FORBIDDEN"
UNKNOWN = "UNKNOWN"

ALLOW = "ALLOW"
REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
BLOCK = "BLOCK"

# --- real tools only: keys must exist in TOOL_REGISTRY ---------------------
TOOL_RISK: dict[str, str] = {
    "check_leave_balance": READ_ONLY,
    "get_employee_requests": READ_ONLY,
    "get_it_tickets": READ_ONLY,
    "extract_entities": READ_ONLY,
    "create_leave_request": HIGH_RISK,
    "create_it_ticket": HIGH_RISK,
}

# --- destructive admin semantics: no executor, never routable --------------
FORBIDDEN_ACTIONS: frozenset[str] = frozenset({
    "delete_audit_history",
    "mass_disable_users",
    "remove_aduser_wildcard",
})


def risk_of(action_key: str) -> str:
    """Risk level for a tool or policy-level action name.

    Unknown names return UNKNOWN (fail closed downstream).
    """
    if action_key in FORBIDDEN_ACTIONS:
        return FORBIDDEN
    return TOOL_RISK.get(action_key, UNKNOWN)


def policy_gate(action_key: str) -> dict:
    """Single policy decision point used by the Phase A benchmark harness.

    Returns {"action", "risk", "gate", "reason"} where gate is one of
    ALLOW / REQUIRE_APPROVAL / BLOCK. FORBIDDEN and UNKNOWN both BLOCK:
    approval can never promote them.
    """
    risk = risk_of(action_key)
    if risk == READ_ONLY:
        return {"action": action_key, "risk": risk, "gate": ALLOW,
                "reason": "read-only: no side effects"}
    if risk == LOW_RISK:
        return {"action": action_key, "risk": risk, "gate": ALLOW,
                "reason": "low-risk under existing application policy"}
    if risk == HIGH_RISK:
        return {"action": action_key, "risk": risk, "gate": REQUIRE_APPROVAL,
                "reason": "side-effect tool: C1 confirm-before-action"}
    if risk == FORBIDDEN:
        return {"action": action_key, "risk": risk, "gate": BLOCK,
                "reason": "forbidden: no executor, approval cannot override"}
    return {"action": action_key, "risk": UNKNOWN, "gate": BLOCK,
            "reason": "unknown action: fail closed"}


def requires_approval(action_key: str) -> bool:
    """True only for HIGH_RISK. FORBIDDEN/UNKNOWN are BLOCK, not approval."""
    return risk_of(action_key) == HIGH_RISK


def is_forbidden(action_key: str) -> bool:
    """True for FORBIDDEN actions. Approval must not override this."""
    return risk_of(action_key) == FORBIDDEN
