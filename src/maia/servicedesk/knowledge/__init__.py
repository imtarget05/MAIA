"""Knowledge base adapters (T3/T4): ingestion, evidence context, sources.

Guarantees enforced here rather than trusted from callers:
- tenant and ACL come from server-side state (``Principal`` + this module),
  never from client-supplied metadata;
- every ingest produces an immutable (doc_id, version) row and only becomes
  ``active`` after its chunks are persisted, so a failed index can never
  publish a half-ingested version;
- Qdrant point IDs are deterministic UUID5 over tenant/doc/version/chunk, so
  re-indexing is idempotent and vectors can be rebuilt from PostgreSQL.
"""
from __future__ import annotations

from maia.servicedesk.knowledge.context_budget import (
    MAX_CHARS,
    TOP_K,
    build_evidence_context,
    cap_chunks,
    truncate,
)
from maia.servicedesk.knowledge.citations import read_source
from maia.servicedesk.knowledge.ingest import (
    UnsupportedDocument,
    ingest_document,
    revoke_document,
)

__all__ = [
    "MAX_CHARS",
    "TOP_K",
    "UnsupportedDocument",
    "build_evidence_context",
    "cap_chunks",
    "ingest_document",
    "read_source",
    "revoke_document",
    "truncate",
]