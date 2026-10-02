"""Conversation durable state on PostgreSQL (DS-5/DS-6).

WHY A PORT. `maia.agent.session.SessionStore` is a concrete SQLite singleton that
business code imports directly (`workflow_orchestrator`, `agentic`), so there is
no seam to swap a durable backend behind. This module introduces that seam —
`ConversationRepository` — and implements it over the tables created by Alembic
revision `3_durable_shared_state`. The SQLite store stays untouched and remains
the explicit local-mode implementation; nothing here deletes or rewrites it.

TENANT ISOLATION IS IN THE SQL, NOT IN PYTHON. Every read and write carries
`tenant_id` in the WHERE clause, so a cross-tenant query returns zero rows and
Tenant A's data never enters this process's object space. The alternative —
fetch by id, then compare `row.tenant_id` in Python — returns the right verdict
for the test while still moving the row across the boundary.

A cross-tenant miss is `None`, not an exception: "not found" and "not yours"
must be indistinguishable, or the API becomes an existence oracle for other
tenants' ids.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from .durable_models import Conversation, ConversationMessage


class ConversationNotFound(LookupError):
    """No conversation for this (tenant, session).

    Raised on writes and returned as `None` on reads on purpose: a caller must
    not be able to tell "this session does not exist" from "this session belongs
    to someone else".
    """

    def __init__(self, tenant_id: str, session_id: str) -> None:
        super().__init__(
            f"conversation not found for tenant={tenant_id!r} session={session_id!r}"
        )
        self.tenant_id = tenant_id
        self.session_id = session_id


@runtime_checkable
class ConversationRepository(Protocol):
    """The semantic operations business code may depend on.

    Deliberately not `Session`: leaking SQLAlchemy's session into callers puts
    query construction back in the business layer, which is how a tenant
    predicate ends up missing from one code path.
    """

    def create(
        self, tenant_id: str, session_id: str, title: str | None = None
    ) -> str: ...

    def get(self, tenant_id: str, session_id: str) -> dict | None: ...

    def append_message(
        self, tenant_id: str, session_id: str, role: str, content: str
    ) -> str: ...

    def list_messages(self, tenant_id: str, session_id: str) -> list[dict]: ...

    def set_title(self, tenant_id: str, session_id: str, title: str) -> bool: ...


class PostgresConversationRepository:
    """`ConversationRepository` over the Alembic-managed durable schema."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, tenant_id: str, session_id: str) -> dict | None:
        """Load one conversation, scoped by BOTH tenant and session id.

        The tenant predicate is not an optimisation. It is the isolation
        boundary: `WHERE session_id = :sid` alone would happily return another
        tenant's row for a colliding session id.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(Conversation).where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.session_id == session_id,
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def list_messages(self, tenant_id: str, session_id: str) -> list[dict]:
        """Messages for one conversation, tenant-scoped.

        Filters on BOTH the denormalised `conversation_messages.tenant_id` and
        the parent conversation's tenant, so a row whose denormalised tenant
        disagrees with its parent is filtered rather than trusted.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(ConversationMessage)
                    .join(
                        Conversation,
                        Conversation.id == ConversationMessage.conversation_id,
                    )
                    .where(
                        ConversationMessage.tenant_id == tenant_id,
                        Conversation.tenant_id == tenant_id,
                        Conversation.session_id == session_id,
                    )
                    .order_by(ConversationMessage.seq)
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def create(self, tenant_id: str, session_id: str, title: str | None = None) -> str:
        now = datetime.now(UTC)
        conversation_id = str(uuid.uuid4())
        with self._engine.begin() as conn:
            conn.execute(
                insert(Conversation).values(
                    id=uuid.UUID(conversation_id),
                    tenant_id=tenant_id,
                    session_id=session_id,
                    title=title,
                    created_at=now,
                    updated_at=now,
                )
            )
        return conversation_id

    def append_message(
        self, tenant_id: str, session_id: str, role: str, content: str
    ) -> str:
        """Append a message, computing `seq` inside the same transaction.

        The unique constraint `(conversation_id, seq)` is the real guard. Two
        concurrent appends would both read the same max, and the loser failing on
        the constraint is the correct outcome (the caller retries) rather than a
        silently interleaved transcript.
        """
        now = datetime.now(UTC)
        message_id = str(uuid.uuid4())
        with self._engine.begin() as conn:
            conversation = conn.execute(
                select(Conversation.id).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.session_id == session_id,
                )
            ).scalar_one_or_none()
            if conversation is None:
                # No conversation for THIS tenant. Refusing here is what stops a
                # cross-tenant append from creating an orphan message row.
                raise ConversationNotFound(tenant_id, session_id)
            current = conn.execute(
                select(ConversationMessage.seq)
                .where(ConversationMessage.conversation_id == conversation)
                .order_by(ConversationMessage.seq.desc())
                .limit(1)
            ).scalar_one_or_none()
            conn.execute(
                insert(ConversationMessage).values(
                    id=uuid.UUID(message_id),
                    tenant_id=tenant_id,
                    conversation_id=conversation,
                    role=role,
                    content=content,
                    seq=0 if current is None else current + 1,
                    created_at=now,
                )
            )
        return message_id

    def set_title(self, tenant_id: str, session_id: str, title: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                update(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.session_id == session_id,
                )
                .values(title=title, updated_at=datetime.now(UTC))
            )
        return bool(result.rowcount)

    def delete(self, tenant_id: str, session_id: str) -> bool:
        """Tenant-scoped delete. Used by tests and by retention jobs."""
        if self.get(tenant_id, session_id) is None:
            return False
        with self._engine.begin() as conn:
            conn.execute(
                delete(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.session_id == session_id,
                )
            )
        return True


def build_repository(engine: Engine) -> ConversationRepository:
    """Single construction point, so the choice of backend is explicit."""
    return PostgresConversationRepository(engine)
