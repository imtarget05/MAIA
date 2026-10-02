"""Durable shared-state schema for PostgreSQL (DS-4).

WHY A SEPARATE MODULE FROM `models.py`. That one holds the auth/RBAC schema,
created with `create_all` at API startup against a SQLite file. `create_all`
cannot express schema evolution — it adds missing tables but never a column — and
a SQLite file cannot be shared by replicas. This module is the durable half that
PostgreSQL owns, and it is Alembic-managed (see `migrations/`).

TENANT KEYING IS NOT OPTIONAL. Every tenant-owned table carries `tenant_id`, and
it participates in indexes and unique constraints, so isolation is enforced by
the DATABASE at query time rather than by a Python check after the row has
already been fetched. The composite keys below are the enforcement point.

NO CACHED PAYLOADS. Deliberately absent: retrieval cache, embeddings, raw
prompts and documents. Those are reconstructible or belong to the vector store;
putting them here would make PostgreSQL the wrong cache.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    """Timezone-aware UTC now.

    `models.py` uses naive `datetime.utcnow`, deprecated in 3.12+. Durable state
    uses aware timestamps so an instance in another timezone cannot shift
    audit-event ordering.
    """
    return datetime.now(UTC)


class DurableBase(DeclarativeBase):
    """Separate metadata from the auth schema on purpose.

    One shared `Base` would let `create_all` on the auth database drag durable
    tables along with it, which is how a schema you cannot migrate ends up in
    production.
    """


class Conversation(DurableBase):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    # (tenant_id, session_id) IS the application-level identity of a conversation.
    # Tenant-scoped uniqueness is deliberate: two tenants may legitimately use the
    # same session_id, and a globally unique one would leak existence.
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "session_id", name="uq_conversations_tenant_session"
        ),
        Index("ix_conversations_tenant_id", "tenant_id"),
    )


class ConversationMessage(DurableBase):
    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalised deliberately: a message read must be filterable by tenant
    # without resolving its parent first, and an FK cannot guarantee the parent
    # belongs to the same tenant.
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        Index("ix_messages_tenant_conversation", "tenant_id", "conversation_id"),
    )


class AgentRun(DurableBase):
    """One agent execution. Checkpoints hang off this, not off a process."""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The graph's thread id. Not unique alone: two tenants may pick the same
    # thread id, and a global unique index would both fail and leak existence.
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "thread_id", name="uq_agent_runs_tenant_thread"),
        Index("ix_agent_runs_tenant_status", "tenant_id", "status"),
    )


class AgentCheckpoint(DurableBase):
    """Durable agent state.

    JSONB rather than TEXT so a checkpoint can be queried during incident
    review. Bounded by the caller: an unbounded prompt or document dumped into a
    checkpoint turns PostgreSQL into a document store with no retention policy.
    """

    __tablename__ = "agent_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    # Resume needs "latest checkpoint for this run"; monotonic seq makes that a
    # single indexed lookup and makes duplicate writes detectable.
    __table_args__ = (
        UniqueConstraint("agent_run_id", "seq", name="uq_checkpoints_run_seq"),
        Index("ix_checkpoints_tenant_run", "tenant_id", "agent_run_id"),
    )


class ApprovalRequest(DurableBase):
    """HITL approval truth, durable (DS-10).

    Process memory was the previous home for a pending proposal, so an approval
    created on replica A was invisible to replica B and lost on restart.
    """

    __tablename__ = "approval_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # Proposal arguments. Secrets must never land here: this row is readable by
    # anything with tenant-scoped access and is retained for audit.
    proposal: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_approvals_tenant_status", "tenant_id", "status"),
        Index("ix_approvals_tenant_created", "tenant_id", "created_at"),
    )


class IdempotencyRecord(DurableBase):
    """Durable side-effect idempotency (DS-11).

    Redis is NOT sufficient: a record that evaporates on restart re-allows a side
    effect that already happened. The unique constraint is the correctness
    mechanism, not the read path — two concurrent claimants race on INSERT and
    exactly one wins.

    Tenant-scoped so two tenants using the same external key cannot collide, and
    so neither can observe the other's operations.
    """

    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="in_progress"
    )
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_type",
            "idempotency_key",
            name="uq_idempotency_scope_key",
        ),
    )


class AuditEvent(DurableBase):
    """Append-only lifecycle record (DS-12).

    Ids, status and timestamps only — never JWTs, API keys, DB passwords or raw
    prompts. `detail` is a small operator-facing dict; a payload that grows beyond
    what an auditor needs belongs elsewhere.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)


class JobRecord(DurableBase):
    """Ingestion / async job metadata.

    Status lives here so a job's outcome survives the worker that ran it. The
    message body itself is not durable truth and is not stored.
    """

    __tablename__ = "job_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[str] = mapped_column(String(128), nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (
        # One row per (tenant, job_type, document): a redelivered Event Grid
        # notification collapses onto the existing row instead of causing a
        # second ingestion of the same document.
        UniqueConstraint(
            "tenant_id", "job_type", "document_id", name="uq_jobs_tenant_type_document"
        ),
        Index("ix_jobs_tenant_status", "tenant_id", "status"),
    )
