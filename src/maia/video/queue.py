"""DB-backed job queue primitives (T3) — lease / heartbeat / retry / DLQ.

Pattern mirrors the archived Kafka pipeline semantics (at-least-once +
idempotent output) and the Service Desk lease model, without a broker:

- lease: claim a queued job with ``FOR UPDATE SKIP LOCKED`` (Postgres) so N
  workers never grab the same row.
- heartbeat: a leased job not updated within ``lease_seconds`` is requeued —
  no job can be stranded by a dead worker.
- retry: capped by ``max_attempts`` then a DLQ row in ``vid_jobs_failed``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from maia.video.models import VideoJob, VideoJobFailure
from maia.video.schemas import LeasedJob


def _now() -> datetime:
    # naive-UTC convention (repo policy DTZ003/DTZ005)
    return datetime.utcnow()


class Queue:
    """Lease/complete/fail operations over the ``vid_jobs`` table."""

    def __init__(self, factory: sessionmaker[Session], *, lease_seconds: int = 300,
                 max_attempts: int = 3, clock=None) -> None:
        self.factory = factory
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._clock = clock or _now

    # -- operations ----------------------------------------------------------

    def lease_next(self, worker_id: str) -> LeasedJob | None:
        """Claim the oldest queued job; bump ``attempt``; stamp the lease."""
        now = self._clock()
        with self.factory() as session:
            stmt = (
                select(VideoJob)
                .where(VideoJob.status == "queued")
                .order_by(VideoJob.created_at, VideoJob.id)
                .limit(1)
            )
            # Row-lock only where supported (Postgres). SQLite (offline tests)
            # serializes writers anyway — FOR UPDATE is not valid there.
            if session.get_bind().dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            job = session.execute(stmt).scalars().first()
            if job is None:
                return None
            job.status = "leased"
            job.leased_by = worker_id
            job.attempt = (job.attempt or 0) + 1
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.heartbeat_at = now
            new_lease = (job.lease or 0) + 1  # fencing token
            job.lease = new_lease
            session.commit()
            return LeasedJob(
                job_id=job.id,
                lease=new_lease,
                tenant_id=job.tenant_id,
                source_uri=job.source_uri,
                preset=job.preset or {},
                attempt=job.attempt,
            )

    def heartbeat(self, job_id: Any, lease: int, worker_id: str) -> bool:
        """Extend the lease; ignore stale holders (fencing token mismatch)."""
        now = self._clock()
        with self.factory() as session:
            job = session.get(VideoJob, job_id)
            if job is None or job.status != "leased" or (job.lease or 0) != lease:
                return False
            if job.leased_by != worker_id:
                return False
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            session.commit()
            return True

    def complete(
        self, job_id: Any, lease: int, *, output_uri: str, metadata: dict | None
    ) -> bool:
        with self.factory() as session:
            job = session.get(VideoJob, job_id)
            if job is None or job.status != "leased" or (job.lease or 0) != lease:
                return False  # lease lost — output stays idempotent by job_id
            job.status = "done"
            job.output_uri = output_uri
            job.metadata_json = metadata
            job.error = None
            job.leased_by = None
            job.lease_expires_at = None
            job.finished_at = self._clock()
            session.commit()
            return True

    def fail(self, job_id: Any, lease: int, error: str) -> str:
        """Record a failure: requeue for retry or DLQ when attempts exhausted.

        Returns the new status: ``queued`` (retry) or ``failed`` (terminal).
        """
        with self.factory() as session:
            job = session.get(VideoJob, job_id)
            if job is None or (job.lease or 0) != lease:
                return "unknown"
            if job.attempt >= self.max_attempts:
                job.status = "failed"
                job.error = error
                job.leased_by = None
                job.lease_expires_at = None
                job.finished_at = self._clock()
                session.add(
                    VideoJobFailure(
                        job_id=job.id,
                        tenant_id=job.tenant_id,
                        error=error,
                        attempts=job.attempt,
                    )
                )
                session.commit()
                return "failed"
            job.status = "queued"
            job.error = error
            job.leased_by = None
            job.lease_expires_at = None
            session.commit()
            return "queued"

    def requeue_expired(self) -> int:
        """Requeue leases that expired (dead worker recovery)."""
        now = self._clock()
        with self.factory() as session:
            result = session.execute(
                update(VideoJob)
                .where(
                    VideoJob.status == "leased",
                    VideoJob.lease_expires_at.isnot(None),
                    VideoJob.lease_expires_at <= now,
                )
                .values(status="queued", leased_by=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    def get(self, job_id: Any) -> VideoJob | None:
        with self.factory() as session:
            return session.get(VideoJob, job_id)

    def count_by_status(self) -> dict[str, int]:
        with self.factory() as session:
            rows = session.execute(
                select(VideoJob.status, func.count()).group_by(VideoJob.status)
            ).all()
            return {status: int(n) for status, n in rows}
