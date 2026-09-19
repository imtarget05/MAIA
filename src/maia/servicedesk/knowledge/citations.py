"""Citation source access (T3/T4).

Reading a source re-checks the authoritative PostgreSQL metadata (tenant,
ACL, active version, tombstone) BEFORE returning any text. A citation is not
a capability: holding a chunk_id never grants access to its content, and a
revoked document stops resolving immediately (plan T3/S6).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from maia.servicedesk.models import SDChunk, SDDocument
from maia.servicedesk.policies import OutOfScope, can_read_document
from maia.servicedesk.schemas import Principal


def read_source(
    session: Session, principal: Principal, chunk_id: str
) -> dict[str, Any]:
    """Return the authorized chunk payload for a Citation.

    Raises ``OutOfScope`` when the chunk does not exist in the caller's
    tenant or the document is no longer readable (revoked, superseded
    version, or outside the caller's groups) — the API maps this to 404 so
    scope is never disclosed.
    """
    chunk = session.execute(
        select(SDChunk).where(
            SDChunk.chunk_id == chunk_id,
            SDChunk.tenant_id == principal.tenant_id,
        )
    ).scalars().first()
    if chunk is None:
        raise OutOfScope(f"unknown source {chunk_id!r}")

    document = session.get(SDDocument, chunk.document_id)
    if document is None or not can_read_document(principal, document):
        raise OutOfScope(f"source {chunk_id!r} is not readable")

    # Chunk/version consistency: never serve text from a superseded version.
    if document.version != chunk.document_version or document.deleted_at is not None:
        raise OutOfScope(f"source {chunk_id!r} belongs to an inactive version")

    metadata = chunk.chunk_metadata or {}
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": str(document.id),
        "document_version": document.version,
        "doc_id": document.doc_id,
        "title": document.title,
        "section": metadata.get("section"),
        "page": metadata.get("page"),
        "text": chunk.text,
        "visibility": document.visibility,
    }


def source_identity(document_id: UUID, version: int, chunk_id: str) -> str:
    """Immutable identity used to validate citations attached to a draft."""
    return f"{document_id}:{version}:{chunk_id}"
