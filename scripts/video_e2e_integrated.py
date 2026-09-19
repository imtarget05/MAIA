"""End-to-end integration check for the video pipeline (INTEGRATED mode).

Verifies the full production path that offline tests cannot cover:

    Alembic migration -> MinIO upload -> vid_jobs enqueue -> VideoWorker
    (real ffmpeg) -> output in MinIO -> presigned download URL

Prerequisites (see deploy/docker/compose.video.yml):
    docker compose -f deploy/docker/compose.video.yml up -d postgres minio
    pip install alembic psycopg[binary] minio  (plus requirements-video.txt)

Usage:
    python scripts/video_e2e_integrated.py [--keep]

Exit code 0 = integration pass. `--keep` leaves containers/data intact.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

POSTGRES_URL = "postgresql+psycopg://maia:maia@localhost:5434/maia"
MINIO_ENDPOINT = "localhost:9000"


def sh(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, **kwargs)


def step(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(1, 60 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep containers running")
    args = parser.parse_args()

    from maia.video.db import make_engine, make_session_factory
    from maia.video.models import VideoJob
    from maia.video.pipeline import probe_metadata
    from maia.video.queue import Queue
    from maia.video.schemas import compute_idempotency_key
    from maia.video.settings import Settings
    from maia.video.storage import MinioStorage
    from maia.video.worker import VideoWorker

    step("1. Settings: integrated-mode validator must accept this config")
    settings = Settings(
        mode="integrated",
        database_url=POSTGRES_URL,
        storage_backend="minio",
        storage_dir="./storage/video",
        minio_endpoint=MINIO_ENDPOINT,
        minio_access_key="minioadmin",
        minio_secret_key="minioadmin",
        encoder_profile="auto",
        _env_file=None,
    )
    problems = settings.readiness_errors()
    assert problems == [], f"readiness problems: {problems}"
    print("readiness_errors == []  OK")

    step("2. Alembic migration to head on real PostgreSQL")
    import os

    env = dict(os.environ, VID_DATABASE_URL=POSTGRES_URL)
    sh(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic_video.ini"),
         "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
        env=env,
    )

    step("3. Generate a 2s test clip (real ffmpeg) and upload to MinIO")
    tmp = Path(tempfile.mkdtemp(prefix="vid_e2e_"))
    clip = tmp / "in.mp4"
    sh(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=2:size=1280x720:rate=24",
         "-f", "lavfi", "-i", "sine=duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(clip)],
        check=True,
    )
    storage = MinioStorage(settings)
    sha = __import__("hashlib").sha256(clip.read_bytes()).hexdigest()
    source_uri = storage.upload(str(clip), f"sources/{sha[:16]}/in.mp4")
    print("source uploaded:", source_uri)
    src_meta = probe_metadata(str(clip))
    print("source metadata:", src_meta)

    step("4. Enqueue job into vid_jobs (real PostgreSQL)")
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    preset = {"job_type": "transcode", "resolution": "480p", "codec": "h264",
              "format": "mp4", "bitrate_kbps": 1000}
    key = compute_idempotency_key(sha, preset)
    # Re-runnable: clear this script's own previous rows/objects first.
    with factory() as s:
        from sqlalchemy import delete

        old = s.execute(
            delete(VideoJob).where(VideoJob.tenant_id == "e2e-integration")
        )
        s.commit()
        if old.rowcount:
            print(f"cleaned {old.rowcount} previous e2e row(s)")
    with factory() as s:
        job = VideoJob(
            tenant_id="e2e-integration",
            idempotency_key=key,
            status="queued",
            source_uri=source_uri,
            source_sha256=sha,
            preset=preset,
            max_attempts=settings.max_attempts,
        )
        s.add(job)
        s.commit()
        job_id = job.id
    print("job enqueued:", job_id)

    step("5. Worker processes the job (real ffmpeg, real Postgres lease)")
    queue = Queue(factory, lease_seconds=settings.lease_seconds,
                  max_attempts=settings.max_attempts)
    worker = VideoWorker(settings, queue, storage, worker_id="e2e-worker")
    assert worker.step() is True, "worker processed nothing"

    step("6. Verify: job done + output in MinIO + presigned URL")
    with factory() as s:
        row = s.get(VideoJob, job_id)
        assert row is not None
        assert row.status == "done", f"status={row.status} error={row.error}"
        assert row.metadata_json and row.metadata_json.get("width") == 1280
        out_uri = row.output_uri
    print("final status: done | output:", out_uri)
    keys = storage.list_output_keys(str(job_id))
    assert keys, "no output objects found in MinIO"
    print("minio objects:", keys)
    out_key = out_uri.removeprefix("s3://")
    presigned = storage.presign_download(out_key, expires_sec=300)
    assert presigned.startswith("http"), presigned
    print("presigned URL OK (truncated):", presigned.split("?")[0], "...")

    print("\nINTEGRATION PASS ✅  (postgres + minio + ffmpeg + alembic + worker)")
    if args.keep:
        print("note: docker containers left running (--keep)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
