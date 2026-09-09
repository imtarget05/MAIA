"""Enterprise tools - mock HR/IT actions with tenant authorization.

Base implementation used by hris.py (real HRIS + mock fallback).
All functions accept an optional ``tenant_id`` and enforce employee→tenant
ownership when ``settings.TOOL_TENANT_CHECK`` is True.
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

from ..config import settings

# --- Mock HR DB ---
_DEFAULT_BALANCE = 12


def _load_hr_db() -> dict:
    p = Path(settings.HR_MOCK_DB_PATH)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_hr_db(db: dict) -> None:
    p = Path(settings.HR_MOCK_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(db, ensure_ascii=False, indent=2))


def _employee_tenant_lookup(employee_id: str, db: dict | None = None) -> str | None:
    """Return the tenant that owns an employee in the mock DB, or None if unknown."""
    if db is None:
        db = _load_hr_db()
    emp = db.get(employee_id)
    if not isinstance(emp, dict):
        return None
    t = emp.get("tenant_id")
    return t if t else None


def _authorize_employee(employee_id: str, tenant_id: str | None,
                         *, persist_unknown: bool = False) -> tuple[bool, str | None]:
    """Fail-closed tenant ownership check.

    Returns (authorized, reason). Skipped (→ allowed) when ``tenant_id`` is None
    or ``TOOL_TENANT_CHECK`` is disabled.
    """
    if tenant_id is None or not settings.TOOL_TENANT_CHECK:
        return True, None
    db = _load_hr_db()
    emp = db.get(employee_id)
    if not isinstance(emp, dict):
        if persist_unknown:
            set_employee_tenant(employee_id, tenant_id)
        return True, None
    emp_tenant = emp.get("tenant_id")
    if not emp_tenant:
        if persist_unknown:
            set_employee_tenant(employee_id, tenant_id)
        return True, None
    if emp_tenant != tenant_id:
        return False, "unauthorized: employee not in tenant"
    return True, None


def _unauthorized_result(employee_id: str, tenant_id: str | None, reason: str = "unauthorized") -> dict:
    return {"ok": False, "error": reason, "employee_id": employee_id,
            "tenant_id": tenant_id}


def set_employee_tenant(employee_id: str, tenant_id: str) -> None:
    """Test/seed helper: tag an employee record with a tenant in the mock DB."""
    db = _load_hr_db()
    emp = db.setdefault(employee_id, {"balance": _DEFAULT_BALANCE, "requests": []})
    if not isinstance(emp, dict):
        emp = {"balance": _DEFAULT_BALANCE, "requests": []}
    emp["tenant_id"] = tenant_id
    _save_hr_db(db)


def check_leave_balance(employee_id: str = "emp_001", tenant_id: str | None = None) -> dict:
    ok, reason = _authorize_employee(employee_id, tenant_id, persist_unknown=False)
    if not ok:
        return _unauthorized_result(employee_id, tenant_id, reason)
    db = _load_hr_db()
    emp = db.get(employee_id, {"balance": _DEFAULT_BALANCE, "requests": []})
    balance = emp.get("balance", _DEFAULT_BALANCE)
    return {"employee_id": employee_id, "balance": balance, "unit": "days",
            "tenant_id": tenant_id or _employee_tenant_lookup(employee_id) or settings.TENANT_ID}


def create_leave_request(employee_id: str = "emp_001", days: int = 1,
                           start_date: str | None = None,
                           tenant_id: str | None = None) -> dict:
    ok, reason = _authorize_employee(employee_id, tenant_id, persist_unknown=True)
    if not ok:
        return _unauthorized_result(employee_id, tenant_id, reason)
    if days <= 0 or days > 30:
        return {"ok": False, "error": "Số ngày nghỉ không hợp lệ (1-30)."}
    bal = check_leave_balance(employee_id, tenant_id=tenant_id)
    if days > bal["balance"]:
        return {"ok": False, "error": f"Không đủ số ngày phép. Còn lại {bal['balance']} ngày, yêu cầu {days} ngày."}
    db = _load_hr_db()
    emp = db.setdefault(employee_id, {"balance": _DEFAULT_BALANCE, "requests": []})
    emp["balance"] = emp.get("balance", _DEFAULT_BALANCE) - days
    rid = f"LV-{datetime.now().strftime('%Y%m%d')}-{random.randint(100,999)}"
    rec = {"request_id": rid, "employee_id": employee_id, "days": days,
           "start_date": start_date or "TBD", "status": "pending_manager_approval",
           "created_at": datetime.now().isoformat()}
    emp["requests"].append(rec)
    _save_hr_db(db)
    return {"ok": True, "request_id": rid, "days": days, "start_date": start_date,
            "remaining_balance": emp["balance"], "status": "pending_manager_approval",
            "tenant_id": tenant_id or _employee_tenant_lookup(employee_id) or settings.TENANT_ID}


def create_it_ticket(employee_id: str = "emp_001", ticket_type: str = "general",
                       description: str = "", tenant_id: str | None = None) -> dict:
    ok, reason = _authorize_employee(employee_id, tenant_id, persist_unknown=True)
    if not ok:
        return _unauthorized_result(employee_id, tenant_id, reason)
    prefix = {"vpn_request": "VPN", "lost_device": "SEC", "laptop_broken": "IT"}.get(ticket_type, "IT")
    tid = f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{random.randint(100,999)}"
    return {"ok": True, "ticket_id": tid, "type": ticket_type,
            "employee_id": employee_id, "description": description,
            "status": "open", "assignee": "IT Help Desk ext 202",
            "tenant_id": tenant_id or _employee_tenant_lookup(employee_id) or settings.TENANT_ID}


def get_employee_requests(employee_id: str = "emp_001", tenant_id: str | None = None) -> list[dict]:
    ok, reason = _authorize_employee(employee_id, tenant_id)
    if not ok:
        return []
    db = _load_hr_db()
    return db.get(employee_id, {}).get("requests", [])


TOOL_REGISTRY = {
    "check_leave_balance": check_leave_balance,
    "create_leave_request": create_leave_request,
    "create_it_ticket": create_it_ticket,
    "get_employee_requests": get_employee_requests,
}

