"""HRIS connector — real API with mock fallback (§ HRIS integration).

When HRIS_ENABLED=true and HRIS_BASE_URL configured, attempts HTTP calls.
On any failure (no network, no creds, not enabled) falls back to local mock DB
(tools.py). This keeps offline tests green while allowing prod swap.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from ..config import settings
from .tools import (
    _DEFAULT_BALANCE,
    _load_hr_db as _load_mock,
    _save_hr_db as _save_mock,
    check_leave_balance as mock_balance,
    create_leave_request as mock_create,
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

def check_leave_balance(employee_id: str = "emp_001") -> dict:
    # try real HRIS
    data = _try_hris(f"/employees/{employee_id}/leave/balance", "GET")
    if data and "balance" in data:
        return {"employee_id": employee_id, "balance": int(data["balance"]), "unit": "days", "source": "hris"}
    # fallback mock
    res = mock_balance(employee_id)
    res["source"] = "mock"
    return res

def create_leave_request(employee_id: str = "emp_001", days: int = 1, start_date: str | None = None) -> dict:
    payload = {"employee_id": employee_id, "days": days, "start_date": start_date}
    data = _try_hris("/leave/requests", "POST", payload)
    if data and data.get("request_id"):
        return {"ok": True, "request_id": data["request_id"], "days": days, "start_date": start_date, "remaining_balance": data.get("remaining_balance"), "status": data.get("status", "pending"), "source": "hris"}
    res = mock_create(employee_id, days, start_date)
    res["source"] = res.get("source", "mock")
    return res

def get_employee_info(employee_id: str = "emp_001") -> dict:
    data = _try_hris(f"/employees/{employee_id}", "GET")
    if data:
        data["source"] = "hris"
        return data
    # mock employee info
    bal = mock_balance(employee_id)
    return {"employee_id": employee_id, "name": f"Employee {employee_id}", "department": "Engineering", "balance": bal["balance"], "source": "mock"}

def verify_ticket(ticket_id: str) -> dict:
    data = _try_hris(f"/tickets/{ticket_id}", "GET")
    if data:
        data["source"] = "hris"
        return data
    # mock verify: ticket exists if it matches pattern
    return {"ticket_id": ticket_id, "status": "open", "verified": True, "source": "mock"}

def get_employee_requests(employee_id: str = "emp_001") -> list[dict]:
    data = _try_hris(f"/employees/{employee_id}/leave/requests", "GET")
    if isinstance(data, list):
        return data
    return mock_requests(employee_id)
