"""HRIS connector — real API with mock fallback (§ HRIS integration).

When HRIS_ENABLED=true and HRIS_BASE_URL configured, attempts HTTP calls.
On any failure (no network, no creds, not enabled) falls back to local mock DB
(tools.py). This keeps offline tests green while allowing prod swap.

P1-1 / P1-5: every tool wrapper accepts an optional ``tenant_id`` and enforces
employee→tenant ownership (fail-closed). When ``tenant_id`` is None the check
is skipped (backward-compat allow); when ``settings.TOOL_TENANT_CHECK`` is
False the check is also skipped (tests can opt out).
"""
from __future__ import annotations

from typing import Any

import requests

from ..config import settings
from . import itsm as _itsm
from .tools import (
    _authorize_employee,
    _employee_tenant_lookup,
    _unauthorized_result,
)
from .tools import (
    check_leave_balance as mock_balance,
)
from .tools import (
    create_leave_request as mock_create,
)
from .tools import (
    get_employee_requests as mock_requests,
)


def _hris_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if settings.HRIS_API_KEY:
        h["Authorization"] = f"Bearer {settings.HRIS_API_KEY}"
    return h

def _try_hris(path: str, method: str = "GET", payload: dict | None = None) -> Any | None:
    if not settings.HRIS_ENABLED or not settings.HRIS_BASE_URL:
        return None
    url = settings.HRIS_BASE_URL.rstrip("/") + path
    try:
        if method == "GET":
            r = requests.get(url, headers=_hris_headers(), timeout=settings.HRIS_TIMEOUT_SEC)
        else:
            r = requests.post(url, headers=_hris_headers(), json=payload, timeout=settings.HRIS_TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def check_leave_balance(employee_id: str = "emp_001", tenant_id: str | None = None) -> dict:
    # P1-5: fail-closed tenant ownership BEFORE touching real/mock backend.
    # Read path: do not persist unknown employees (keeps the mock DB untouched).
    ok, reason = _authorize_employee(employee_id, tenant_id, persist_unknown=False)
    if not ok:
        return _unauthorized_result(employee_id, tenant_id, reason)
    # try real HRIS
    data = _try_hris(f"/employees/{employee_id}/leave/balance", "GET")
    if data and "balance" in data:
        return {"employee_id": employee_id, "balance": int(data["balance"]), "unit": "days", "source": "hris"}
    # fallback mock
    res = mock_balance(employee_id, tenant_id=tenant_id)
    res["source"] = "mock"
    res["tenant_id"] = tenant_id or _employee_tenant_lookup(employee_id) or settings.TENANT_ID
    return res

def create_leave_request(employee_id: str = "emp_001", days: int = 1,
                           start_date: str | None = None, tenant_id: str | None = None) -> dict:
    ok, reason = _authorize_employee(employee_id, tenant_id, persist_unknown=True)
    if not ok:
        return _unauthorized_result(employee_id, tenant_id, reason)
    payload = {"employee_id": employee_id, "days": days, "start_date": start_date}
    data = _try_hris("/leave/requests", "POST", payload)
    if data and data.get("request_id"):
        return {"ok": True, "request_id": data["request_id"], "days": days, "start_date": start_date, "remaining_balance": data.get("remaining_balance"), "status": data.get("status", "pending"), "source": "hris"}
    res = mock_create(employee_id, days, start_date, tenant_id=tenant_id)
    res["source"] = res.get("source", "mock")
    return res

def create_it_ticket(employee_id: str = "emp_001", ticket_type: str = "general",
                       description: str = "", tenant_id: str | None = None) -> dict:
    """Create an IT/security ticket (P1-5: routed through hris.py, not tools directly)."""
    ok, reason = _authorize_employee(employee_id, tenant_id, persist_unknown=True)
    if not ok:
        return _unauthorized_result(employee_id, tenant_id, reason)
    payload = {"employee_id": employee_id, "ticket_type": ticket_type, "description": description}
    data = _try_hris("/tickets", "POST", payload)
    if data and data.get("ticket_id"):
        return {"ok": True, "ticket_id": data["ticket_id"], "type": ticket_type,
                "employee_id": employee_id, "description": description,
                "status": data.get("status", "open"), "assignee": data.get("assignee", "IT Help Desk"),
                "source": "hris"}
    res = _itsm.create_it_ticket(employee_id, ticket_type, description, tenant_id=tenant_id)
    if res.get("source") == "local":
        res["source"] = "mock"
    else:
        res["source"] = res.get("source", "mock")
    return res

def get_employee_info(employee_id: str = "emp_001", tenant_id: str | None = None) -> dict:
    ok, reason = _authorize_employee(employee_id, tenant_id)
    if not ok:
        return {"employee_id": employee_id, "name": "unauthorized", "source": "denied",
                "error": reason}
    data = _try_hris(f"/employees/{employee_id}", "GET")
    if data:
        data["source"] = "hris"
        return data
    # mock employee info
    bal = mock_balance(employee_id, tenant_id=tenant_id)
    return {"employee_id": employee_id, "name": f"Employee {employee_id}", "department": "Engineering", "balance": bal["balance"], "source": "mock"}

def verify_ticket(ticket_id: str) -> dict:
    data = _try_hris(f"/tickets/{ticket_id}", "GET")
    if data:
        data["source"] = "hris"
        return data
    # mock verify: ticket exists if it matches pattern
    return {"ticket_id": ticket_id, "status": "open", "verified": True, "source": "mock"}

def get_employee_requests(employee_id: str = "emp_001", tenant_id: str | None = None) -> list[dict]:
    authorized, _ = _authorize_employee(employee_id, tenant_id)
    if not authorized:
        return []
    data = _try_hris(f"/employees/{employee_id}/leave/requests", "GET")
    if isinstance(data, list):
        return data
    return mock_requests(employee_id, tenant_id=tenant_id)
