"""Department email notifications (stdlib only, no new deps).

Routing: leave workflows -> HR inbox, IT tickets/VPN/security -> IT inbox.
When SMTP is not configured (SMTP_HOST empty, the default), mails are queued
to STORAGE_DIR/outbox.json instead of being sent — the admin dashboard shows
the outbox, so nothing is silently lost. Every send attempt is recorded in
the workflow activity feed by the caller.
"""
from __future__ import annotations

import json
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from .config import settings

# request/intent type -> department inbox
DEPARTMENT_ROUTING: dict[str, str] = {
    "create_leave_request": "hr",
    "leave_request": "hr",
    "leave_balance": "hr",
    "hr_policy": "hr",
    "expense": "hr",
    "benefits": "hr",
    "onboarding": "hr",
    "create_it_ticket": "it",
    "it_help": "it",
    "vpn": "it",
    "security": "security",
}


def department_inbox(dept: str) -> str:
    return {
        "hr": settings.HR_EMAIL,
        "it": settings.IT_EMAIL,
        "security": settings.SECURITY_EMAIL,
    }.get(dept, settings.HR_EMAIL)


def inbox_for(request_type: str) -> tuple[str, str]:
    """(department, inbox email) for a workflow request type."""
    dept = DEPARTMENT_ROUTING.get(request_type, "hr")
    return dept, department_inbox(dept)


def _outbox_path() -> Path:
    p = Path(settings.STORAGE_DIR) / "outbox.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_outbox(limit: int = 100) -> list[dict]:
    try:
        items = json.loads(_outbox_path().read_text(encoding="utf-8"))
        return items[-limit:][::-1]
    except Exception:
        return []


def _queue_outbox(entry: dict) -> None:
    try:
        path = _outbox_path()
        items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        items.append(entry)
        path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def send_email(to: str, subject: str, body: str, *, kind: str = "workflow") -> dict:
    """Send one email; queue to outbox when SMTP is missing or fails.

    Returns {"ok": True, "via": "smtp"|"outbox", ...}. Never raises.
    """
    entry = {"to": to, "subject": subject, "body": body, "kind": kind,
             "ts": datetime.utcnow().isoformat() + "Z"}
    if not settings.SMTP_HOST:
        entry["via"] = "outbox"
        _queue_outbox(entry)
        return {"ok": True, "via": "outbox", "queued": True}
    try:
        msg = EmailMessage()
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False)
        msg.set_content(body)
        if settings.SMTP_USE_TLS:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
        try:
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        entry["via"] = "smtp"
        _queue_outbox(entry)
        return {"ok": True, "via": "smtp"}
    except Exception as e:  # never break the request flow on mail failure
        entry["via"] = "outbox"
        entry["smtp_error"] = f"{type(e).__name__}: {e}"
        _queue_outbox(entry)
        return {"ok": False, "via": "outbox", "queued": True, "error": entry["smtp_error"]}


def notify_new_request(request_type: str, summary: str, requester: str,
                       detail: str = "") -> dict:
    """Alert the owning department that a request awaits approval."""
    dept, inbox = inbox_for(request_type)
    subject = f"[MAIA] Yêu cầu mới chờ duyệt: {summary}"
    body = (f"Loại: {request_type} (bộ phận: {dept})\n"
            f"Người yêu cầu: {requester}\n"
            f"Nội dung: {summary}\n"
            + (f"Chi tiết: {detail}\n" if detail else "") +
            "Vui lòng vào Dashboard MAIA để duyệt/từ chối.")
    return send_email(inbox, subject, body, kind="workflow_new")


def notify_request_decided(request_type: str, summary: str, requester: str,
                           approved: bool, decided_by: str,
                           result_ref: str = "") -> dict:
    """Tell the requester (and CC the department) about the decision."""
    dept, inbox = inbox_for(request_type)
    verdict = "ĐÃ DUYỆT ✅" if approved else "ĐÃ TỪ CHỐI ❌"
    subject = f"[MAIA] Yêu cầu {verdict}: {summary}"
    body = (f"Kết quả: {verdict} (người duyệt: {decided_by})\n"
            f"Người yêu cầu: {requester}\n"
            f"Nội dung: {summary}\n"
            + (f"Mã tham chiếu: {result_ref}\n" if result_ref else "") +
            f"Bộ phận liên quan: {dept} ({inbox})")
    r1 = send_email(requester, subject, body, kind="workflow_decided") if "@" in (requester or "") else {"ok": False, "skipped": True}
    r2 = send_email(inbox, subject, body, kind="workflow_decided")
    return {"ok": bool(r1.get("ok") or r2.get("ok")), "requester": r1, "department": r2}


def send_password_reset_email(to: str, reset_link: str) -> dict:
    subject = "[MAIA] Đặt lại mật khẩu"
    body = (f"Bạn (hoặc ai đó) đã yêu cầu đặt lại mật khẩu MAIA.\n\n"
            f"Link đặt lại (hết hạn sau {settings.PASSWORD_RESET_EXPIRE_MIN} phút):\n{reset_link}\n\n"
            f"Nếu không phải bạn, hãy bỏ qua email này.")
    return send_email(to, subject, body, kind="password_reset")
