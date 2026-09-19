"""Ticket/document/approval visibility and authority (S4/T1).

Rules (plan S4/S6):
- Read scope: a requester sees only tickets they raised; agents/supervisors
  see tickets assigned to their support groups; admin does NOT bypass
  tenant scoping.
- Out-of-scope reads return "not found" semantics; mutations without
  authority raise ``NotAuthorized`` (mapped to 403 by the API layer).
- Server-side grants only: every check takes a server-resolved Principal;
  nothing here trusts client/LLM-supplied role or group data.
"""
from __future__ import annotations

from maia.servicedesk.models import SDTicket
from maia.servicedesk.schemas import Principal


class NotAuthorized(PermissionError):
    """Caller lacks authority for the mutation (API maps to 403)."""


class OutOfScope(LookupError):
    """Resource exists but is outside the caller's tenant/scope (-> 404)."""


def can_read_ticket(principal: Principal, ticket: SDTicket) -> bool:
    if ticket.tenant_id != principal.tenant_id or ticket.quarantined:
        return False
    role = principal.role
    if role == "requester":
        return ticket.requester_external_id is not None and _owns_ticket(
            principal, ticket
        )
    if role in ("agent", "supervisor"):
        return ticket.support_group in principal.group_ids
    if role == "admin":
        # Admin manages configuration, not unrestricted ticket reads (S3).
        return ticket.support_group in principal.group_ids
    return False


def _owns_ticket(principal: Principal, ticket: SDTicket) -> bool:
    # v1: requester ownership is resolved via the admin-confirmed external
    # identity mapping at projection time. Unmapped tickets stay internal
    # and invisible to requesters (S6 rule).
    return ticket.requester_user_id == principal.user_id


def authorize_proposal(principal: Principal, ticket: SDTicket, kind: str) -> None:
    if ticket.tenant_id != principal.tenant_id or ticket.quarantined:
        raise OutOfScope("ticket outside caller scope")
    if not ticket.support_group or ticket.support_group not in principal.group_ids:
        raise NotAuthorized("caller is not in the ticket's support group")
    if principal.role == "agent":
        if kind != "add_comment":
            raise NotAuthorized(
                "agent may only approve add_comment; routing/priority need a supervisor"
            )
    elif principal.role not in ("supervisor", "admin"):
        raise NotAuthorized(f"role {principal.role!r} cannot approve actions")
