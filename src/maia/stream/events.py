"""Chunk / failure event schemas for the Kafka pipeline (spec §3, §8).

    ChunkEvent   -> produced by parser onto topic.doc.chunks
    FailedEvent  -> produced to topic.doc.chunks.dlq / topic.doc.embedding.failed
"""
import hashlib
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _uuid() -> str:
    return str(uuid.uuid4())


class ChunkEvent(BaseModel):
    """A single chunk ready for embedding (spec §3)."""

    event_id: str = Field(default_factory=_uuid)
    document_id: str
    chunk_id: str
    chunk_index: int = 0
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    attempt: int = 0  # how many times embedding has been attempted

    # ---- partitioning keys (spec §4) ------------------------------------
    def partition_key(self, mode: str) -> str:
        """Return the Kafka message key for the given partitioning mode."""
        if mode == "max-throughput":
            # key = hash(document_id + chunk_id) -> spread across partitions.
            return hashlib.sha1(f"{self.document_id}|{self.chunk_id}".encode()).hexdigest()
        # default: ordered -> key = document_id -> all chunks of a doc to 1 partition.
        return self.document_id


class FailedEvent(BaseModel):
    """A chunk that exhausted retries and was routed to a DLQ (spec §8)."""

    chunk_id: str
    document_id: str
    error: str
    attempt: int
    timestamp: str = Field(default_factory=_now)