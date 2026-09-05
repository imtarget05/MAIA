"""Enterprise tools - mock HR/IT actions."""
from __future__ import annotations

import json
import random
import re
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


def check_leave_balance(employee_id: str = "emp_001") -> dict:
    db = _load_hr_db()
    emp = db.get(employee_id, {"balance": _DEFAULT_BALANCE, "requests": []})
    balance = emp.get("balance", _DEFAULT_BALANCE)
    return {"employee_id": employee_id, "balance": balance, "unit": "days"}

def create_leave_request(employee_id: str = "emp_001", days: int = 1, start_date: str | None = None) -> dict:
    if days <= 0 or days > 30:
        return {"ok": False, "error": "Số ngày nghỉ không hợp lệ (1-30)."}
    bal = check_leave_balance(employee_id)
    if days > bal["balance"]:
        return {"ok": False, "error": f"Không đủ số ngày phép. Còn lại {bal['balance']} ngày, yêu cầu {days} ngày."}
    db = _load_hr_db()
    emp = db.setdefault(employee_id, {"balance": _DEFAULT_BALANCE, "requests": []})
    # deduct
    emp["balance"] = emp.get("balance", _DEFAULT_BALANCE) - days
    rid = f"LV-{datetime.now().strftime('%Y%m%d')}-{random.randint(100,999)}"
    rec = {"request_id": rid, "employee_id": employee_id, "days": days, "start_date": start_date or "TBD", "status": "pending_manager_approval", "created_at": datetime.now().isoformat()}
    emp["requests"].append(rec)
    _save_hr_db(db)
    return {"ok": True, "request_id": rid, "days": days, "start_date": start_date, "remaining_balance": emp["balance"], "status": "pending_manager_approval"}

def create_it_ticket(employee_id: str = "emp_001", ticket_type: str = "general", description: str = "") -> dict:
    prefix = {"vpn_request": "VPN", "lost_device": "SEC", "laptop_broken": "IT"}.get(ticket_type, "IT")
    tid = f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{random.randint(100,999)}"
    return {"ok": True, "ticket_id": tid, "type": ticket_type, "employee_id": employee_id, "description": description, "status": "open", "assignee": "IT Help Desk ext 202"}

def get_employee_requests(employee_id: str = "emp_001") -> list[dict]:
    db = _load_hr_db()
    return db.get(employee_id, {}).get("requests", [])


TOOL_REGISTRY = {
    "check_leave_balance": check_leave_balance,
    "create_leave_request": create_leave_request,
    "create_it_ticket": create_it_ticket,
}
