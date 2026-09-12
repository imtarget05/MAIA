"""ITSM connector — real provider with local fallback.

When ITSM_ENABLED=true and ITSM_BASE_URL is set, attempts a real provider
call (default provider: jira). On any failure falls back to the local JSON
audit store in tools.py. Offline tests stay green; prod swaps with env only.

Architecture (single source of truth — see hr_interface.py):
- tools.py (mock + local JSON persist) is the fallback implementation.
- This module owns the "try real ITSM first" decision for IT tickets.
- hris.py:create_it_ticket routes through here (HRIS stays HR-only).
"""
from __future__ import annotations

from typing import Any, Protocol

import requests

from ..config import settings
from .tools import (
    _authorize_employee,
    _unauthorized_result,
)
from .tools import (
    create_it_ticket as mock_it_ticket,
)

_TICKET_PRIORITY: dict[str, str] = {
    "lost_device": "high",
    "vpn_request": "normal",
    "laptop_broken": "normal",
    "general": "low",
}

_TICKET_COMPONENT: dict[str, str] = {
    "lost_device": "Security",
    "vpn_request": "Access",
    "laptop_broken": "Hardware",
    "general": "General",
}


def priority_for(ticket_type: str) -> str:
    return _TICKET_PRIORITY.get(ticket_type, "low")


def component_for(ticket_type: str) -> str:
    return _TICKET_COMPONENT.get(ticket_type, "General")


class ITSMProvider(Protocol):
    """Adapter contract every ITSM backend must satisfy."""

    name: str

    def create_ticket(self, *, employee_id: str, ticket_type: str,
                      description: str) -> dict | None:
        """Return at least {"ticket_id": ...} or None to fall back."""
        ...


def _itsm_headers(token: str) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class JiraProvider:
    """Jira Service Management — real REST call when configured.

    POST {base}/rest/api/3/issue with a minimal fields payload. Any
    transport/API error returns None so the caller falls back to local
    storage (never raises).
    """

    name = "jira"

    def create_ticket(self, *, employee_id: str, ticket_type: str,
                      description: str) -> dict | None:
        if not settings.ITSM_ENABLED or not settings.ITSM_BASE_URL:
            return None
        url = settings.ITSM_BASE_URL.rstrip("/") + "/rest/api/3/issue"
        project = settings.ITSM_PROJECT_KEY or "IT"
        payload = {
            "fields": {
                "project": {"key": project},
                "summary": f"[{ticket_type}] {employee_id}: "
                           f"{(description or '')[:80]}",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [{
                        "type": "paragraph",
                        "content": [{
                            "type": "text",
                            "text": (f"Reporter: {employee_id}\n"
                                     f"Type: {ticket_type} "
                                     f"(priority={priority_for(ticket_type)}, "
                                     f"component={component_for(ticket_type)})\n"
                                     f"Description: {description or ''}"),
                        }],
                    }],
                },
                "issuetype": {"name": "Task"},
                "priority": {"name": priority_for(ticket_type).capitalize()},
                "labels": [ticket_type, f"reporter-{employee_id}"],
            }
        }
        try:
            r = requests.post(url, headers=_itsm_headers(settings.ITSM_API_TOKEN),
                              json=payload, timeout=settings.ITSM_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json()
            key = data.get("key") or data.get("id")
            if not key:
                return None
            return {"ticket_id": str(key), "status": "open",
                    "assignee": "Jira queue", "source": "itsm:jira"}
        except Exception:
            return None


class _NotConfiguredProvider:
    """Placeholder for providers not wired yet (fail-soft to fallback)."""

    def __init__(self, provider_name: str) -> None:
        self.name = provider_name

    def create_ticket(self, *, employee_id: str, ticket_type: str,
                      description: str) -> dict | None:
        return None


PROVIDERS: dict[str, Any] = {
    "jira": JiraProvider(),
    "servicenow": _NotConfiguredProvider("servicenow"),
    "zendesk": _NotConfiguredProvider("zendesk"),
}


def get_provider(name: str | None = None) -> Any:
    key = (name or settings.ITSM_PROVIDER or "jira").lower()
    return PROVIDERS.get(key, PROVIDERS["jira"])


def create_it_ticket(employee_id: str = "emp_001", ticket_type: str = "general",
                     description: str = "", tenant_id: str | None = None) -> dict:
    """Create an IT/security ticket (ITSM-first, local fallback).

    Fail-closed tenant ownership BEFORE touching any backend. On the real
    path the local JSON audit row is still appended (tools.py persists +
    resolves tenant), so get_it_tickets stays single-sourced.
    """
    ok, reason = _authorize_employee(employee_id, tenant_id, persist_unknown=True)
    if not ok:
        return _unauthorized_result(employee_id, tenant_id, reason)
    provider = get_provider()
    try:
        data = provider.create_ticket(employee_id=employee_id,
                                      ticket_type=ticket_type,
                                      description=description)
    except Exception:
        data = None
    if data and data.get("ticket_id"):
        local = mock_it_ticket(employee_id, ticket_type, description,
                               tenant_id=tenant_id)
        local.update({
            "ticket_id": data["ticket_id"],
            "status": data.get("status", local.get("status", "OPEN")),
            "assignee": data.get("assignee", local.get("assignee")),
            "source": data.get("source", f"itsm:{provider.name}"),
        })
        return local
    res = mock_it_ticket(employee_id, ticket_type, description, tenant_id=tenant_id)
    res["source"] = res.get("source", "local")
    res["itsm_provider"] = provider.name
    return res

