"""Typed contracts for the video pipeline (T1) — API + worker boundary.

Pydantic-only, no legacy MAIA agent imports (Service Desk convention).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

JobType = Literal["transcode", "hls_vod", "remux", "extract_audio"]
JobStatus = Literal["queued", "leased", "done", "failed"]
OutputFormat = Literal["mp4", "hls"]


class VideoJobCreate(BaseModel):
    """POST /video/jobs body."""

    model_config = ConfigDict(extra="forbid")

    source_uri: str = Field(min_length=1, max_length=512)
    source_sha256: str | None = Field(default=None, max_length=64)
    job_type: JobType = "transcode"
    # Output resolution for transcode ("720p", "480p", ...). None = keep.
    resolution: str | None = None
    bitrate_kbps: int | None = Field(default=None, ge=100, le=100_000)
    codec: Literal["h264", "h265"] = "h264"
    format: OutputFormat = "mp4"

    def to_preset(self) -> dict[str, Any]:
        return {
            "job_type": self.job_type,
            "resolution": self.resolution,
            "bitrate_kbps": self.bitrate_kbps,
            "codec": self.codec,
            "format": self.format,
        }


def compute_idempotency_key(source_sha256: str, preset: dict[str, Any]) -> str:
    """Stable key: hash(source_sha256 + canonical preset).

    Two identical submissions (same file + same preset) collapse to one job.
    """
    import hashlib
    import json

    canonical = json.dumps(preset, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{source_sha256}|{canonical}".encode()
    ).hexdigest()


class VideoJobEvent(BaseModel):
    """Public job state — returned by the API and consumed by the worker."""

    job_id: UUID
    tenant_id: str
    status: JobStatus
    source_uri: str
    preset: dict[str, Any]
    output_uri: str | None = None
    metadata_json: dict[str, Any] | None = None
    error: str | None = None
    attempt: int = 0
    created_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> VideoJobEvent:
        return cls(
            job_id=row.id,
            tenant_id=row.tenant_id,
            status=row.status,
            source_uri=row.source_uri,
            preset=row.preset or {},
            output_uri=row.output_uri,
            metadata_json=row.metadata_json,
            error=row.error,
            attempt=row.attempt,
            created_at=row.created_at,
            finished_at=row.finished_at,
        )


class LeasedJob(BaseModel):
    """Hand-off from ``lease_next`` to the executor loop."""

    job_id: UUID
    lease: int
    tenant_id: str = "default"
    source_uri: str
    preset: dict[str, Any]
    attempt: int
