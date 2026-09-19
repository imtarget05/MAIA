"""Service Desk domain tables (S4) — all prefixed ``sd_``, Postgres-only.

Key invariants encoded here:
- Tenant consistency via composite FKs where a child row references a
  tenant-owned parent (ticket/proposal/draft chains).
- UNIQUE(integration_id, external_key) so two Jira integrations can never
  mix the same issue key; local API only exposes canonical UUIDs (T2).
- Drafts are revisioned (never overwritten in place); proposals are
  immutable content + CAS state transitions; the outbox is one row per
  approved proposal with a unique command_key (S4, T6/T7).
- ``sd_jobs`` carries due-time claiming fields (available_at + lease token)
  per the SmartDoc port-concept review in S1/S5 (T5/T7).
- ``sd_audit_events`` is append-only at the application layer (audit.py).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class SDRole(str, enum.Enum):
    REQUESTER = "requester"
    AGENT = "agent"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"


class SDUser(Base):
    """Service Desk account. Password hashing reuses maia.auth primitives."""

    __tablename__ = "sd_users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SDMembership(Base):
    """Server-side grant: role + group within one tenant (never client-supplied)."""

    __tablename__ = "sd_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),
        CheckConstraint(
            "role IN ('requester','agent','supervisor','admin')",
            name="ck_membership_role",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_users.id"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    group_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SDIntegration(Base):
    """One Jira Cloud connection. Secret stored by reference, never inlined."""

    __tablename__ = "sd_integrations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="jira")
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    project_key: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    secret_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


# --- S4 continued: tickets, comments, identities, drafts ---


class SDTicket(Base):
    """Projection of a linked Jira issue. Jira owns content/status; the local
    ``version`` advances with each applied remote snapshot (T2 CAS rule)."""

    __tablename__ = "sd_tickets"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "external_key", name="uq_ticket_integration_external"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_ticket_id_tenant"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_integrations.id"), nullable=False
    )
    external_key: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    requester_external_id: Mapped[str | None] = mapped_column(String(128))
    # Local owner resolved via admin-confirmed external identity mapping
    # (nullable until mapped; unmapped tickets stay internal per S6).
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_users.id")
    )
    support_group: Mapped[str | None] = mapped_column(String(64))
    remote_status: Mapped[str | None] = mapped_column(String(64))
    remote_priority: Mapped[str | None] = mapped_column(String(64))
    # timestamptz: compared against Jira's offset-aware timestamps for the
    # stale-snapshot CAS rule (T2). Naive columns would raise TypeError on
    # comparison (found by test_remote_old_snapshot_cannot_revert_ticket).
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SDTicketComment(Base):
    __tablename__ = "sd_ticket_comments"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "external_id", name="uq_comment_integration_external"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_integrations.id"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(128))
    author_external_id: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    # timestamptz for the same offset-aware reason as SDTicket.remote_updated_at.
    remote_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SDExternalIdentity(Base):
    """Admin-confirmed mapping from a Jira account to a local user."""

    __tablename__ = "sd_external_identities"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "external_account_id",
            name="uq_extid_integration_account",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_integrations.id"), nullable=False
    )
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    local_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_users.id"), nullable=False
    )
    confirmed_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SDDraft(Base):
    """Revisioned draft; edits append a new revision (never overwrite)."""

    __tablename__ = "sd_drafts"
    __table_args__ = (UniqueConstraint("id", "revision", name="uq_draft_id_revision"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False)
    triage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    model_name: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_users.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


# --- part 3: proposals, jobs ---


class SDProposal(Base):
    """Immutable command proposal; approved != delivered (S4 state machine)."""

    __tablename__ = "sd_proposals"
    __table_args__ = (
        CheckConstraint(
            "state IN ('proposed','approved','rejected','expired','superseded')",
            name="ck_proposal_state",
        ),
        UniqueConstraint("id", "tenant_id", name="uq_proposal_id_tenant"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    immutable_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_drafts.id")
    )
    draft_revision: Mapped[int | None] = mapped_column(Integer)
    requester_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_users.id")
    )
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_users.id")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class SDJob(Base):
    """Durable job row: due-time claim, lease fencing, error taxonomy (S7)."""

    __tablename__ = "sd_jobs"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_job_dedup_key"),
        CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_job_state",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_ref: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    error_class: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    dedup_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


# --- part 4: inbox, outbox, audit ---


class SDInboxEvent(Base):
    """Signed webhook deliveries; UNIQUE(integration, delivery) for dedup (T8)."""

    __tablename__ = "sd_inbox_events"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "delivery_id", name="uq_inbox_integration_delivery"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    integration_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_integrations.id"), nullable=False
    )
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)


class SDOutbox(Base):
    """One durable write command per approved proposal (T6/T7)."""

    __tablename__ = "sd_outbox"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_outbox_proposal"),
        UniqueConstraint("command_key", name="uq_outbox_command_key"),
        CheckConstraint(
            "state IN ('queued','executing','retry_wait','outcome_unknown',"
            "'succeeded','failed')",
            name="ck_outbox_state",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sd_proposals.id"), nullable=False
    )
    command_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    external_ref: Mapped[str | None] = mapped_column(String(128))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_class: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SDAuditEvent(Base):
    """Append-only audit (application layer enforces insert-only)."""

    __tablename__ = "sd_audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_role: Mapped[str | None] = mapped_column(String(16))
    entity_id: Mapped[str | None] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
