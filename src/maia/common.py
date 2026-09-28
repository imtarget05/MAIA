"""Small shared helpers: timestamps and content-addressed ids.

Kept dependency-free and in one place because the MCP servers, the ingestion
pipeline and the eval reports all need *identical* id semantics: an id derived
from content is how a retried call becomes idempotent and how an audit record is
correlated with the payload that produced it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

__all__ = ["content_id", "now_iso", "stable_key"]


def now_iso() -> str:
    """UTC timestamp, second-precision ISO-8601 with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_key(*parts: Any) -> str:
    """Deterministic short key from values (idempotency / dedupe)."""
    blob = json.dumps([str(p) for p in parts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def content_id(prefix: str, *parts: Any) -> str:
    """Human-readable, content-addressed id: ``TASK-1a2b3c4d5e6f7a8b``."""
    return f"{prefix.upper()}-{stable_key(*parts)}"
