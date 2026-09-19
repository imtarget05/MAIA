"""Ticket projection ingestion (T2).

Rules (S4):
- Jira owns issue content; PostgreSQL holds a projection with
  ``remote_updated_at`` and a locally increasing ``version``.
- A stale remote snapshot can NEVER revert a newer projection (T2 CAS test):
  snapshots whose ``updated`` timestamp is not strictly newer are ignored.
- Same ``external_key`` under two integrations stays two separate tickets
  (UNIQUE(integration_id, external_key)).
- The API layer only exposes canonical local UUIDs; issue keys never become
  a read path by themselves.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from maia.servicedesk.models import SDIntegration, SDTicket


def _parse_remote_updated(raw: Any) -> datetime:
    # Jira timestamps look like 2026-09-19T12:59:20.539+0000.
    text = str(raw).replace("+0000", "+00:00").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class TicketService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_snapshot(
        self, integration_id: UUID, snapshot: dict[str, Any]
    ) -> UUID:
        integration = self.session.get(SDIntegration, integration_id)
        if integration is None:
            raise ValueError(f"unknown integration {integration_id}")

        external_key = snapshot.get("key")
        fields = snapshot.get("fields") or {}
        summary = fields.get("summary")
        if not external_key or not summary:
            raise ValueError("snapshot must carry key and summary")

        remote_updated = _parse_remote_updated(fields.get("updated"))
        existing = self.session.execute(
            select(SDTicket).where(
                SDTicket.integration_id == integration_id,
                SDTicket.external_key == external_key,
            )
        ).scalar_one_or_none()

        if existing is not None:
            current_updated = existing.remote_updated_at
            if current_updated is not None and remote_updated <= current_updated:
                # Stale/echo snapshot: never revert a newer projection (T2).
                return existing.id
            self._apply(existing, fields, remote_updated, snapshot)
            existing.version += 1
            return existing.id

        ticket = SDTicket(
            tenant_id=integration.tenant_id,
            integration_id=integration_id,
            external_key=external_key,
            summary=summary,
            version=1,
        )
        self._apply(ticket, fields, remote_updated, snapshot)
        self.session.add(ticket)
        self.session.flush()
        return ticket.id

    def _apply(
        self,
        ticket: SDTicket,
        fields: dict[str, Any],
        remote_updated: datetime,
        snapshot: dict[str, Any],
    ) -> None:
        ticket.summary = fields.get("summary") or ticket.summary
        ticket.description = _description_text(fields.get("description"))
        ticket.remote_status = _name_of(fields.get("status"))
        ticket.remote_priority = _name_of(fields.get("priority"))
        reporter = fields.get("reporter") or {}
        ticket.requester_external_id = reporter.get("accountId")
        ticket.remote_updated_at = remote_updated
        ticket.quarantined = False

    def get_version(self, ticket_id: UUID) -> int | None:
        ticket = self.session.get(SDTicket, ticket_id)
        return ticket.version if ticket else None

    def get_ticket(self, ticket_id: UUID) -> SDTicket | None:
        return self.session.get(SDTicket, ticket_id)

    def list_for_tenant(self, tenant_id: str) -> list[SDTicket]:
        return list(
            self.session.execute(
                select(SDTicket)
                .where(SDTicket.tenant_id == tenant_id)
                .order_by(SDTicket.remote_updated_at.desc())
            ).scalars()
        )


def _name_of(field_value: Any) -> str | None:
    if isinstance(field_value, dict):
        return field_value.get("name")
    return None


def _description_text(description: Any) -> str | None:
    """Flatten Jira ADF (Atlassian Document Format) to plain text."""
    if description is None:
        return None
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        parts: list[str] = []

        def walk(node: dict) -> None:
            if node.get("type") == "text":
                parts.append(str(node.get("text", "")))
            for child in node.get("content", []) or []:
                if isinstance(child, dict):
                    walk(child)

        walk(description)
        return "\n".join(part for part in parts if part).strip() or None
    return None
