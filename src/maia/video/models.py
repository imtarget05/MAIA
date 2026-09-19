"""Video pipeline domain tables — all prefixed ``vid_`` (T1).

Own ``Base`` (separate from legacy MAIA and Service Desk) so the video schema
never inherits or mutates other modules' tables. Constraints encoded here:

- ``vid_jobs.status`` CHECK-gated to queued/leased/done/failed (DB-queue).
- UNIQUE(idempotency_key): the same (source, preset) submitted twice cannot
  enqueue two jobs — duplicate submits update the existing row instead
  (API layer handles the conflict transparently).
- Preset/output are JSON so new formats (HLS ladders etc.) need no migration.
- Lease fields (leased_by/lease_expires_at/heartbeat_at) drive the worker
  lease/heartbeat protocol; ``lease`` is the monotonic fencing token.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Generic types with Postgres variants: JSONB + native UUID in production
# (PostgreSQL), plain JSON/CHAR on SQLite for offline tests (create_all).
JsonPG = JSON().with_variant(JSONB(), "postgresql")
UuidPG = Uuid(as_uuid=True).with_variant(PGUUID(as_uuid=True), "postgresql")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UuidPG, primary_key=True, default=uuid.uuid4)


class VideoJob(Base):
    """One transcode/encode job in the DB-backed queue."""

    __tablename__ = "vid_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','leased','done','failed')",
            name="ck_vid_job_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_vid_jobs_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # hash(source_sha256 + preset) — duplicate submits collapse to one job.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")

    # Input
    source_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    source_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    # {"job_type": "transcode"|"hls_vod"|..., "resolution": "720p",
    #  "bitrate_kbps": 2500, "codec": "h264", "format": "mp4"|"hls"}
    preset: Mapped[dict] = mapped_column(JsonPG, nullable=False)

    # Output / metadata
    output_uri: Mapped[str | None] = mapped_column(String(512))
    # ffprobe of the SOURCE (duration/resolution/codec) + result summary.
    metadata_json: Mapped[dict | None] = mapped_column(JsonPG)

    # Failure handling
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # Lease / heartbeat (worker protocol)
    leased_by: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease: Mapped[int | None] = mapped_column(Integer)

    # Lifecycle timestamps (naive-UTC convention per repo policy DTZ003/005)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class VideoJobFailure(Base):
    """Terminal failure record (DLQ pattern ported from archived stream DLQ).

    Appended when a job exhausts ``max_attempts``. Rows here are the admin
    replay surface — the job row itself moves to ``failed`` and stays for
    auditing.
    """

    __tablename__ = "vid_jobs_failed"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_vid_jobs_failed_job"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UuidPG, nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
