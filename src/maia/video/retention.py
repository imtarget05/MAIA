"""Retention + metrics glue for the video pipeline (Giai doan 4).

- ``run_retention`` deletes output objects and terminal job rows older than
  ``VID_RETENTION_DAYS`` (default 7). Temp workspaces are removed by the
  worker itself at the end of each job (``cleanup_workspace``).
- ``update_queue_gauges`` feeds Prometheus gauges from queue state; called
  by the worker on idle passes and by the API metrics endpoint.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from maia.video import metrics
from maia.video.models import VideoJob, VideoJobFailure
from maia.video.queue import Queue
from maia.video.storage import Storage

logger = logging.getLogger("maia.video.retention")


def update_queue_gauges(queue: Queue) -> dict[str, int]:
    """Push current queue depth per status into the metrics registry."""
    counts = queue.count_by_status()
    for status in ("queued", "leased", "done", "failed"):
        metrics.set_gauge("maia_video_jobs", counts.get(status, 0), status=status)
    return counts


def run_retention(
    factory: sessionmaker,
    storage: Storage,
    *,
    retention_days: int = 7,
    clock=None,
    limit: int = 500,
) -> dict[str, int]:
    """Delete outputs + terminal rows older than the retention window.

    Returns ``{"outputs_deleted": n, "jobs_deleted": m}``. Never raises for
    individual object deletions — logged and skipped instead.
    """
    now_fn = clock or datetime.utcnow
    cutoff = now_fn() - timedelta(days=retention_days)
    deleted_objects = 0
    with factory() as session:
        rows = session.execute(
            select(VideoJob)
            .where(
                VideoJob.status.in_(["done", "failed"]),
                VideoJob.finished_at.isnot(None),
                VideoJob.finished_at <= cutoff,
            )
            .limit(limit)
        ).scalars().all()
        for job in rows:
            if job.output_uri:
                try:
                    storage.delete(f"outputs/{job.id}")
                except Exception:
                    logger.warning("retention: could not delete output for %s",
                                   job.id, exc_info=True)
            # DLQ rows share the job id — remove them together
            session.execute(
                delete(VideoJobFailure).where(VideoJobFailure.job_id == job.id)
            )
            session.delete(job)
            deleted_objects += 1
        session.commit()
    return {"outputs_deleted": deleted_objects, "jobs_deleted": deleted_objects}
