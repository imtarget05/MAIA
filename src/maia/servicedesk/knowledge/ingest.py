"""Versioned knowledge ingestion (T3).

Tenant and ACL are taken from server-side state: the caller must be a tenant
admin (``authorize_kb_management``) and ``tenant_id`` always comes from the
authenticated ``Principal``. Client-supplied metadata can only choose among
validated options.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from maia.chunking import split_documents
from maia.servicedesk.models import SDChunk, SDDocument
from maia.servicedesk.policies import NotAuthorized, authorize_kb_management
from maia.servicedesk.schemas import Principal

#: Deterministic namespace so point IDs are reproducible from PostgreSQL
#: (tenant/doc/version/chunk) — vectors are a rebuildable index, not truth.
POINT_NAMESPACE = uuid.UUID("6f2b1c8e-4a17-5d3c-9f10-2b7c0a4d8e60")

SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt", ".pdf")
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100


class UnsupportedDocument(ValueError):
    """Document type or content cannot be indexed (e.g. scanned-only PDF)."""


def point_id(tenant_id: str, doc_id: str, version: int, chunk_id: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"{tenant_id}/{doc_id}/{version}/{chunk_id}"))


def content_hash(content: bytes | str) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def extract_text(filename: str, content: bytes | str) -> str:
    """Extract indexable text. Scanned PDFs are rejected, not silently empty."""
    if isinstance(content, str):
        text = content
    else:
        suffix = _suffix_of(filename)
        if suffix == ".pdf":
            text = _extract_pdf_text(content, filename)
        elif suffix in (".md", ".markdown", ".txt"):
            text = content.decode("utf-8", errors="replace")
        else:
            raise UnsupportedDocument(
                f"unsupported file type {suffix!r}; supported: {SUPPORTED_SUFFIXES}"
            )
    if not text or not text.strip():
        raise UnsupportedDocument(
            f"{filename}: no extractable text (scanned/image-only PDFs are not "
            "supported in v1 — plan S3)"
        )
    return text


def _suffix_of(filename: str) -> str:
    lowered = filename.lower()
    for suffix in (".markdown", ".md", ".txt", ".pdf"):
        if lowered.endswith(suffix):
            return suffix
    return ""


def _extract_pdf_text(content: bytes, filename: str) -> str:
    try:
        import io

        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is in the lock
        raise UnsupportedDocument("pypdf is required to ingest PDF runbooks") from exc
    reader = PdfReader(io.BytesIO(content))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if not text.strip():
        raise UnsupportedDocument(
            f"{filename}: PDF has no text layer (scanned documents are "
            "unsupported in v1; plan S3)"
        )
    return text


def _validate_groups(allowed_groups: list[str]) -> list[str]:
    groups = [str(g).strip() for g in (allowed_groups or []) if str(g).strip()]
    if not groups:
        # No global/public documents: an empty ACL would silently expose the
        # document, so it is rejected instead (plan Global Constraints).
        raise ValueError("allowed_groups must list at least one support group")
    return sorted(set(groups))


def _chunk(
    text: str, doc_id: str, version: int, chunk_size: int, chunk_overlap: int
) -> list[dict[str, Any]]:
    """Chunk via MAIA's splitter (S5 reuse) with version-scoped chunk IDs.

    MAIA's splitter already falls back to a deterministic splitter when
    llama-index is absent, so the Service Desk slice need not require it.
    """
    raw = split_documents(
        [{"text": text, "metadata": {"doc_id": doc_id}}],
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(raw):
        body = chunk.text.strip()
        if body:
            chunks.append(
                {
                    # Version-scoped, immutable ID: a citation always resolves
                    # to the exact text that was retrieved.
                    "chunk_id": f"{doc_id}_v{version}_{index}",
                    "text": body,
                }
            )
    if not chunks and text.strip():
        chunks = [{"chunk_id": f"{doc_id}_v{version}_0", "text": text.strip()}]
    return chunks


def ingest_document(
    session: Session,
    principal: Principal,
    *,
    doc_id: str,
    title: str,
    content: bytes | str,
    filename: str,
    allowed_groups: list[str],
    visibility: str = "agent",
    source_kind: str = "synthetic",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> uuid.UUID:
    """Ingest one document version for the caller's tenant.

    Returns the document row UUID. Re-ingesting identical content is
    idempotent (returns the existing row), so a retried upload never creates
    duplicate versions (plan T3 RED case "same document ingest retry").
    """
    authorize_kb_management(principal)
    if not doc_id or not doc_id.strip():
        raise ValueError("doc_id is required")
    if visibility not in ("agent", "requester"):
        raise ValueError(f"invalid visibility {visibility!r}")
    groups = _validate_groups(allowed_groups)

    text = extract_text(filename, content)
    digest = content_hash(content)

    latest = session.execute(
        select(SDDocument)
        .where(
            SDDocument.tenant_id == principal.tenant_id,
            SDDocument.doc_id == doc_id,
        )
        .order_by(SDDocument.version.desc())
    ).scalars().first()

    if latest is not None and latest.content_hash == digest:
        # Same content already ingested for this tenant: idempotent retry.
        return latest.id

    version = (latest.version + 1) if latest is not None else 1
    if latest is not None:
        # Old versions stay (audit + stable citations) but stop being served:
        # retrieval and source reads re-check ``active`` (S6).
        latest.active = False

    document = SDDocument(
        tenant_id=principal.tenant_id,
        doc_id=doc_id,
        version=version,
        title=title,
        source_kind=source_kind,
        content_hash=digest,
        allowed_groups=groups,
        visibility=visibility,
        active=False,  # published only after every chunk persists
        owner_id=principal.user_id,
    )
    session.add(document)
    session.flush()

    for ordinal, chunk in enumerate(
        _chunk(text, doc_id, version, chunk_size, chunk_overlap)
    ):
        session.add(
            SDChunk(
                tenant_id=principal.tenant_id,
                document_id=document.id,
                document_version=version,
                chunk_id=chunk["chunk_id"],
                ordinal=ordinal,
                text=chunk["text"],
                chunk_metadata={
                    "doc_id": doc_id,
                    "title": title,
                    "point_id": point_id(
                        principal.tenant_id, doc_id, version, chunk["chunk_id"]
                    ),
                },
            )
        )

    document.active = True
    session.flush()
    return document.id


def revoke_document(
    session: Session, principal: Principal, document_id: uuid.UUID
) -> bool:
    """Tombstone a document. Effective immediately because retrieval and
    source reads re-check ``active``/``deleted_at`` per request; the vector
    index is cleaned asynchronously (plan T3)."""
    authorize_kb_management(principal)
    document = session.get(SDDocument, document_id)
    if document is None or document.tenant_id != principal.tenant_id:
        return False
    document.active = False
    document.deleted_at = datetime.now(timezone.utc)
    session.flush()
    return True