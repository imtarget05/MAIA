"""Video pipeline REST API (T4).

Mounted into the main MAIA FastAPI app (see ``maia.api``) via
``app.include_router(video_router)``; also exposes ``create_video_app`` for
standalone deployments and tests. Auth re-uses the legacy MAIA JWT stack via
a lazy import (avoids a circular import at module load).

Tenant isolation: every job row carries ``tenant_id`` resolved server-side
from the authenticated user — a tenant can never read another tenant's job.
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from maia.video.db import make_engine, make_session_factory, resolve_database_url
from maia.video.metrics import render_metrics
from maia.video.models import VideoJob
from maia.video.retention import update_queue_gauges
from maia.video.schemas import VideoJobEvent, compute_idempotency_key
from maia.video.settings import Settings
from maia.video.storage import build_storage

logger = logging.getLogger("maia.video.api")

video_router = APIRouter(prefix="/video", tags=["video"])

# Bearer scheme matching the legacy MAIA login endpoint. auto_error=False so
# missing tokens reach get_video_user which raises 401 cleanly (and so the
# standalone test app can override the dependency entirely).
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


def get_video_user(token: str = Depends(oauth2_scheme)) -> Any:
    """Reuse the legacy MAIA JWT auth. Lazy import because ``maia.api``
    includes this router at load time (no circular module dependency)."""
    from maia import api as legacy_api

    gen = legacy_api.get_db()
    session = next(gen)
    try:
        user = legacy_api.get_current_user(token=token, db=session)
        return legacy_api.get_current_active_user(current_user=user)
    finally:
        session.close()
        gen.close()


_engine_cache: dict[str, Any] = {}


def get_video_settings() -> Settings:
    return Settings()


def get_session_factory(settings: Settings = Depends(get_video_settings)):
    url = resolve_database_url(settings)
    factory = _engine_cache.get(url)
    if factory is None:
        factory = make_session_factory(make_engine(url))
        _engine_cache[url] = factory
    return factory


def get_video_db(
    factory=Depends(get_session_factory),
) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _tenant_of(user: Any) -> str:
    tenant = getattr(user, "tenant_id", None) or "default"
    return str(tenant)

_ALLOWED_JOB_TYPES = {"transcode", "hls_vod", "remux", "extract_audio"}
_ALLOWED_RESOLUTIONS = {None, "1080p", "720p", "480p", "360p"}


def _validate(job_type: str, resolution: str | None) -> None:
    if job_type not in _ALLOWED_JOB_TYPES:
        raise HTTPException(422, f"job_type must be one of {sorted(_ALLOWED_JOB_TYPES)}")
    if resolution not in _ALLOWED_RESOLUTIONS:
        valid = sorted(r for r in _ALLOWED_RESOLUTIONS if r is not None)
        raise HTTPException(422, f"resolution must be one of {valid}")


def _read_upload_to_temp(file: UploadFile, max_bytes: int) -> tuple[Path, str]:
    """Stream the upload to a temp file; enforce a hard size cap; sha256."""
    hasher = hashlib.sha256()
    total = 0
    _, tmp_name = tempfile.mkstemp(prefix="vid_upload_", suffix=".bin")
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, f"upload exceeds {max_bytes} bytes")
                hasher.update(chunk)
                out.write(chunk)
        if total == 0:
            raise HTTPException(422, "empty upload")
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, hasher.hexdigest()


@video_router.post("/jobs", status_code=202)
def create_video_job(
    file: UploadFile = File(...),
    job_type: str = Form("transcode"),
    resolution: str | None = Form(None),
    bitrate_kbps: int | None = Form(None, ge=100, le=100_000),
    db: Session = Depends(get_video_db),
    user: Any = Depends(get_video_user),
    settings: Settings = Depends(get_video_settings),
):
    """Enqueue a video processing job (idempotent per file+preset)."""
    _validate(job_type, resolution)
    preset = {
        "job_type": job_type,
        "resolution": resolution,
        "bitrate_kbps": bitrate_kbps,
        "codec": "h264",
        "format": "hls" if job_type == "hls_vod" else "mp4",
    }
    tmp_path, sha256 = _read_upload_to_temp(file, settings.max_upload_mb * 1024 * 1024)
    try:
        idempotency_key = compute_idempotency_key(sha256, preset)
        existing = db.execute(
            select(VideoJob).where(VideoJob.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return {"duplicated": True, "job": VideoJobEvent.from_row(existing)}
        storage = build_storage(settings)
        source_key = f"sources/{sha256[:16]}/{file.filename or 'input.bin'}"
        source_uri = storage.upload(str(tmp_path), source_key)
        job = VideoJob(
            tenant_id=_tenant_of(user),
            idempotency_key=idempotency_key,
            status="queued",
            source_uri=source_uri,
            source_sha256=sha256,
            preset=preset,
            max_attempts=settings.max_attempts,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return {"duplicated": False, "job": VideoJobEvent.from_row(job)}
    finally:
        tmp_path.unlink(missing_ok=True)


def _get_tenant_job(db: Session, job_id: str, user: Any) -> VideoJob:
    try:
        from uuid import UUID as PyUUID

        parsed = PyUUID(job_id)
    except ValueError:
        raise HTTPException(404, "job not found") from None
    job = db.get(VideoJob, parsed)
    if job is None or job.tenant_id != _tenant_of(user):
        # 404 (not 403) — do not leak other tenants' job ids
        raise HTTPException(404, "job not found")
    return job


@video_router.get("/jobs")
def list_video_jobs(
    limit: int = 50,
    status: str | None = None,
    db: Session = Depends(get_video_db),
    user: Any = Depends(get_video_user),
):
    stmt = (
        select(VideoJob)
        .where(VideoJob.tenant_id == _tenant_of(user))
        .order_by(VideoJob.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    if status:
        stmt = stmt.where(VideoJob.status == status)
    rows = db.execute(stmt).scalars().all()
    return {"jobs": [VideoJobEvent.from_row(r) for r in rows]}


@video_router.get("/jobs/{job_id}")
def get_video_job(
    job_id: str,
    db: Session = Depends(get_video_db),
    user: Any = Depends(get_video_user),
):
    return VideoJobEvent.from_row(_get_tenant_job(db, job_id, user))


@video_router.get("/jobs/{job_id}/download")
def download_video_job(
    job_id: str,
    db: Session = Depends(get_video_db),
    user: Any = Depends(get_video_user),
    settings: Settings = Depends(get_video_settings),
):
    """Done jobs only: presigned URL (MinIO) or authenticated stream (local)."""
    job = _get_tenant_job(db, job_id, user)
    if job.status != "done" or not job.output_uri:
        raise HTTPException(409, f"job is {job.status}; no output yet")
    if settings.storage_backend == "minio":
        # Accept raw key, `<bucket>/<key>`, or full `s3://bucket/key` uri —
        # presign_download normalizes all three.
        url = build_storage(settings).presign_download(job.output_uri, expires_sec=900)
        return {"url": url, "expires_sec": 900}
    path = Path(job.output_uri)
    if not path.exists():
        raise HTTPException(410, "output expired (retention policy)")
    media = (
        "application/vnd.apple.mpegurl"
        if path.suffix == ".m3u8"
        else "application/octet-stream"
    )
    return FileResponse(path, media_type=media, filename=path.name)


@video_router.delete("/jobs/{job_id}")
def delete_video_job(
    job_id: str,
    db: Session = Depends(get_video_db),
    user: Any = Depends(get_video_user),
    settings: Settings = Depends(get_video_settings),
):
    job = _get_tenant_job(db, job_id, user)
    try:
        build_storage(settings).delete(f"outputs/{job.id}")
    except Exception:
        logger.warning("could not delete output for %s", job.id, exc_info=True)
    db.delete(job)
    db.commit()
    return {"ok": True, "deleted": job_id}


@video_router.get("/metrics", response_class=PlainTextResponse)
def video_metrics(factory=Depends(get_session_factory)):
    from maia.video.queue import Queue

    update_queue_gauges(Queue(factory))
    return render_metrics()


def create_video_app(settings: Settings | None = None):
    """Standalone app (tests / dedicated video-side API deployment)."""
    from fastapi import FastAPI

    app = FastAPI(title="MAIA Video Pipeline", version="1.0.0")
    app.include_router(video_router)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "maia-video"}

    return app

