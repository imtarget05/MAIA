"""Notification MCP server — email report, MS Teams card, Zalo OA message.

Three channels, one honesty rule: **a dry run must never look like a delivery.**
When a credential/URL is missing (or ``NOTIFICATION_DRY_RUN=true``), the message is
appended to a local outbox and the result carries ``dry_run: true`` /
``delivered: false``. Tests and demos assert on those flags, so a fake delivery
can never be reported as a real one.

Every send is idempotent by content: ``delivery_id`` is a hash of channel +
recipient + subject + body, so a retried agent turn returns the *same* id and the
outbox can prove exactly what was sent when.

Channel-specific details that matter in production:
* **Email** reuses the existing ``SMTP_*`` settings and sends HTML only when the
  payload actually looks like HTML (so a plain-text report is not mangled).
* **Teams** posts a MessageCard to an incoming-webhook; Teams answers HTTP 200
  with the body ``Invalid webhook`` for a dead URL, so the body is checked too.
* **Zalo OA** tokens expire and are refreshed by hand; the server surfaces the API
  error code instead of pretending the message was queued.
"""
from __future__ import annotations

import json
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from ...config import settings
from ..protocol import ToolResult
from ..server import MCPServer
from ._common import append_jsonl, content_id, now_iso

__all__ = ["NotificationDispatcher", "build_notification_server"]

MAX_BODY_CHARS = 20000
_OUTBOX_DEFAULT = "./storage/notification_outbox.jsonl"


def _looks_like_html(text: str) -> bool:
    lowered = (text or "").lstrip().lower()
    return lowered.startswith(("<html", "<div")) or "<br" in lowered[:200]


class NotificationDispatcher:
    """Channel dispatch with outbox-based dry runs.

    SMTP/Teams/Zalo callables are injectable so the delivery path is testable
    without network access, and "did the remote actually accept it" stays in one
    place instead of three.
    """

    def __init__(
        self,
        *,
        outbox_path: str | Path = _OUTBOX_DEFAULT,
        dry_run: bool | None = None,
        smtp_sender: Callable[[EmailMessage], str] | None = None,
        teams_poster: Callable[[str, dict], tuple[int, str]] | None = None,
        zalo_sender: Callable[[dict], tuple[int, dict]] | None = None,
    ) -> None:
        self.outbox_path = Path(outbox_path)
        self.dry_run = settings.NOTIFICATION_DRY_RUN if dry_run is None else dry_run
        self._smtp_sender = smtp_sender
        self._teams_poster = teams_poster
        self._zalo_sender = zalo_sender

    def _record(self, channel: str, payload: dict[str, Any], delivery_id: str,
                reason: str) -> dict[str, Any]:
        append_jsonl(
            self.outbox_path,
            {"delivery_id": delivery_id, "channel": channel, "at": now_iso(),
             "payload": payload, "dry_run": True, "reason": reason},
        )
        return {
            "delivery_id": delivery_id, "channel": channel, "delivered": False,
            "dry_run": True, "reason": reason, "outbox": str(self.outbox_path),
        }

    # ---- channels --------------------------------------------------------
    def send_email(self, recipient: str, subject: str, body: str) -> dict[str, Any]:
        delivery_id = content_id("eml", recipient, subject, body)
        host = settings.SMTP_HOST
        if self.dry_run or not host:
            return self._record(
                "email", {"to": recipient, "subject": subject, "body": body},
                delivery_id, "dry_run" if self.dry_run else "smtp_not_configured",
            )
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.SMTP_FROM
        message["To"] = recipient
        if _looks_like_html(body):
            message.set_content("HTML report — open in an HTML-capable client.")
            message.add_alternative(body, subtype="html")
        else:
            message.set_content(body)

        if self._smtp_sender:
            server_id = self._smtp_sender(message)
        else:
            with smtplib.SMTP(host, settings.SMTP_PORT, timeout=10) as smtp:
                if settings.SMTP_USE_TLS:
                    smtp.starttls()
                if settings.SMTP_USERNAME:
                    smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                smtp.send_message(message)
            server_id = "smtp"
        append_jsonl(
            self.outbox_path,
            {"delivery_id": delivery_id, "channel": "email", "at": now_iso(),
             "to": recipient, "subject": subject, "dry_run": False},
        )
        return {"delivery_id": delivery_id, "channel": "email", "delivered": True,
                "dry_run": False, "server": server_id}

    def send_teams_card(
        self, webhook_url: str, title: str, summary: str, action_url: str = ""
    ) -> dict[str, Any]:
        delivery_id = content_id("tms", webhook_url, title, summary, action_url)
        url = webhook_url or settings.TEAMS_WEBHOOK_URL
        if self.dry_run or not url:
            return self._record(
                "teams",
                {"webhook_url": url, "title": title, "summary": summary,
                 "action_url": action_url},
                delivery_id, "dry_run" if self.dry_run else "webhook_not_configured",
            )
        card: dict[str, Any] = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "title": title,
            "summary": title,
            "text": summary,
        }
        if action_url:
            card["potentialAction"] = [
                {"@type": "OpenUri", "name": "Mở báo cáo",
                 "targets": [{"os": "default", "uri": action_url}]}
            ]
        if self._teams_poster:
            status, body = self._teams_poster(url, card)
        else:
            import httpx

            response = httpx.post(url, json=card, timeout=10.0)
            status, body = response.status_code, response.text
        # Teams returns HTTP 200 with the text "Invalid webhook" for a dead URL,
        # so a status check alone would report a false success.
        if status >= 400 or "invalid webhook" in (body or "").lower():
            return {"delivery_id": delivery_id, "channel": "teams", "delivered": False,
                    "dry_run": False, "error": f"teams_webhook_failed:{status}",
                    "detail": (body or "")[:200]}
        append_jsonl(
            self.outbox_path,
            {"delivery_id": delivery_id, "channel": "teams", "at": now_iso(),
             "title": title, "dry_run": False},
        )
        return {"delivery_id": delivery_id, "channel": "teams", "delivered": True,
                "dry_run": False, "status": status}

    def send_zalo(self, user_ids: list[str], template_id: str,
                  data: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "recipient": {"user_ids": list(user_ids)},
            "template": {"template_id": str(template_id), "template_data": data},
        }
        delivery_id = content_id("zlo", json.dumps(payload, sort_keys=True))
        token = settings.ZALO_OA_ACCESS_TOKEN
        if self.dry_run or not token:
            return self._record(
                "zalo", payload, delivery_id,
                "dry_run" if self.dry_run else "zalo_token_not_configured",
            )
        if self._zalo_sender:
            status, response = self._zalo_sender(payload)
        else:
            import httpx

            http_response = httpx.post(
                settings.ZALO_OA_ENDPOINT, json=payload,
                headers={"access_token": token, "Content-Type": "application/json"},
                timeout=10.0,
            )
            status, response = http_response.status_code, http_response.json()
        if status >= 400 or int(response.get("error", 0) or 0) != 0:
            return {"delivery_id": delivery_id, "channel": "zalo", "delivered": False,
                    "dry_run": False, "error": f"zalo_api_error:{response.get('error')}",
                    "message": str(response.get("message", ""))[:200]}
        append_jsonl(
            self.outbox_path,
            {"delivery_id": delivery_id, "channel": "zalo", "at": now_iso(),
             "user_ids": list(user_ids), "template_id": template_id, "dry_run": False},
        )
        return {"delivery_id": delivery_id, "channel": "zalo", "delivered": True,
                "dry_run": False, "status": status}


def build_notification_server(dispatcher: NotificationDispatcher | None = None) -> MCPServer:
    """Build the ``notification`` server (email / Teams / Zalo OA)."""
    if dispatcher is None:
        dispatcher = NotificationDispatcher(
            outbox_path=settings.NOTIFICATION_OUTBOX_PATH
        )

    server = MCPServer(
        "notification",
        title="Notification channels",
        instructions=(
            "Send a report or alert over email, MS Teams or Zalo OA. Without "
            "credentials the message is written to a local outbox and the result "
            "reports dry_run=true, delivered=false."
        ),
    )

    @server.tool(
        "send_email_report",
        description="Email a report or alert to one recipient.",
        read_only=False,
        destructive=False,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["recipient", "subject", "body"],
            "additionalProperties": False,
            "properties": {
                "recipient": {"type": "string", "minLength": 3, "maxLength": 120},
                "subject": {"type": "string", "minLength": 3, "maxLength": 200},
                "body": {"type": "string", "minLength": 1, "maxLength": MAX_BODY_CHARS},
            },
        },
    )
    def send_email_report(args: dict[str, Any]) -> ToolResult:
        return ToolResult.json(
            dispatcher.send_email(args["recipient"], args["subject"], args["body"])
        )

    @server.tool(
        "send_teams_card",
        description="Post a MessageCard alert to an MS Teams incoming webhook.",
        read_only=False,
        destructive=False,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["title", "summary"],
            "additionalProperties": False,
            "properties": {
                "webhook_url": {"type": "string", "maxLength": 400},
                "title": {"type": "string", "minLength": 3, "maxLength": 160},
                "summary": {"type": "string", "minLength": 3, "maxLength": 2000},
                "action_url": {"type": "string", "maxLength": 400},
            },
        },
    )
    def send_teams_card(args: dict[str, Any]) -> ToolResult:
        result = dispatcher.send_teams_card(
            args.get("webhook_url", ""), args["title"], args["summary"],
            args.get("action_url", ""),
        )
        return ToolResult.json(result, is_error=not result.get("delivered"))

    @server.tool(
        "send_zalo_oa_message",
        description="Send a Zalo OA template message to one or more user ids.",
        read_only=False,
        destructive=False,
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["user_ids", "template_id", "template_data"],
            "additionalProperties": False,
            "properties": {
                "user_ids": {
                    "type": "array", "minItems": 1, "maxItems": 50,
                    "items": {"type": "string", "minLength": 2, "maxLength": 64},
                },
                "template_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "template_data": {"type": "object"},
            },
        },
    )
    def send_zalo_oa_message(args: dict[str, Any]) -> ToolResult:
        result = dispatcher.send_zalo(
            list(args["user_ids"]), args["template_id"], dict(args["template_data"])
        )
        return ToolResult.json(result, is_error=not result.get("delivered"))

    return server


