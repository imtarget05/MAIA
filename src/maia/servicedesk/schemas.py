"""Service Desk contracts (S8) — typed boundaries shared by API and worker.

These types are new in the Service Desk; they intentionally do NOT import
legacy MAIA agent schemas. ``Principal`` is server-resolved only: client- or
LLM-supplied tenant/role/group values are never trusted (Global Constraints).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

SDRole = Literal["requester", "agent", "supervisor", "admin"]
SDCategory = Literal["vpn", "account_access", "application_access", "other"]


@dataclass(frozen=True)
class Principal:
    """Authenticated actor within one tenant, resolved server-side."""

    user_id: UUID
    tenant_id: str
    role: SDRole
    group_ids: frozenset[str]


class Citation(BaseModel):
    chunk_id: str
    document_id: UUID
    document_version: int
    title: str
    section: str | None = None
    page: int | None = None
    excerpt: str


class DraftResult(BaseModel):
    ticket_id: UUID
    ticket_version: int
    body: str
    citations: list[Citation]
    evidence_status: Literal["supported", "insufficient_evidence"]
    category: SDCategory
    suggested_group: str | None
    missing_fields: list[str]


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_ticket_version: int
    draft_id: UUID | None = None
    draft_revision: int | None = None
    kind: Literal["add_comment", "set_priority", "assign_group"]
    payload: dict


class DeliveryReceipt(BaseModel):
    action_id: UUID
    state: Literal[
        "queued", "executing", "retry_wait", "outcome_unknown", "succeeded", "failed"
    ]
    external_ref: str | None = None
    verified_at: datetime | None = None
