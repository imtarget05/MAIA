"""Offline tests for the MAIA video pipeline (T6, plan-20260919-1925).

No Postgres, no MinIO, no real ffmpeg required (a real-ffmpeg integration
test is skipped when the binary is absent). SQLite + fake subprocess runner,
following the repo's offline-test conventions.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from maia.video import metrics as vmetrics
from maia.video.api import create_video_app
from maia.video.db import (
    create_all,
    make_engine,
    make_session_factory,
    resolve_database_url,
)
from maia.video.encoder import resolve_encoder
from maia.video.models import VideoJob, VideoJobFailure
from maia.video.pipeline import (
    PipelineError,
    build_ffmpeg_args,
    probe_metadata,
    run_ffmpeg,
)
from maia.video.queue import Queue
from maia.video.schemas import compute_idempotency_key
from maia.video.settings import Settings
from maia.video.storage import LocalStorage
from maia.video.worker import VideoWorker

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "mode": "offline",
        "database_url": f"sqlite:///{tmp_path}/video.db",
        "storage_dir": str(tmp_path / "storage"),
        "encoder_profile": "libx264",
        "max_attempts": 2,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def env(tmp_path):
    """Engine + factory + storage + queue wired to a fresh sqlite file."""
    settings = _settings(tmp_path)
    engine = make_engine(settings.database_url)
    create_all(engine)
    factory = make_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)
    queue = Queue(factory, lease_seconds=300, max_attempts=2)
    return SimpleNamespace(
        settings=settings, factory=factory, storage=storage,
        queue=queue, tmp_path=tmp_path,
    )


def _enqueue(ns, *, job_type="transcode", resolution="720p", source: Path | None = None,
             tenant="tenant-a", preset_extra=None) -> VideoJob:
    """Insert a queued job (with its source uploaded through storage)."""
    src = source or ns.tmp_path / "src_in.mp4"
    if not src.exists():
        src.write_bytes(b"fake-video-bytes")
    sha = "a" * 64
    preset = {"job_type": job_type, "resolution": resolution, "format": "mp4",
              **(preset_extra or {})}
    uri = ns.storage.upload(str(src), f"sources/{sha[:16]}/input.mp4")
    with ns.factory() as s:
        job = VideoJob(
            tenant_id=tenant,
            idempotency_key=compute_idempotency_key(sha, preset) + job_type,
            status="queued",
            source_uri=uri,
            source_sha256=sha,
            preset=preset,
            max_attempts=2,
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


# ---------------------------------------------------------------------------
# T1 — settings gates & idempotency
# ---------------------------------------------------------------------------


def test_integrated_requires_postgres():
    with pytest.raises(ValueError):
        Settings(mode="integrated", database_url="sqlite:///./v.db", _env_file=None)


def test_integrated_requires_minio_storage():
    with pytest.raises(ValueError):
        Settings(
            mode="integrated",
            database_url="postgresql://u:p@localhost/db",
            storage_backend="local",
            _env_file=None,
        )


def test_offline_defaults(tmp_path):
    s = _settings(tmp_path)
    assert s.encoder_profile == "libx264"
    assert s.storage_backend == "local"
    assert resolve_database_url(s) == s.database_url
    # empty URL + offline -> explicit sqlite file under storage_dir
    s2 = Settings(mode="offline", storage_dir=str(tmp_path / "s2"), _env_file=None)
    assert resolve_database_url(s2).startswith("sqlite:///")
    with pytest.raises(ValueError):
        resolve_database_url(Settings(mode="integrated", _env_file=None))


def test_env_prefix_maps_vid_fields(monkeypatch):
    monkeypatch.setenv("VID_ENCODER_PROFILE", "nvenc")
    monkeypatch.setenv("VID_RETENTION_DAYS", "3")
    s = Settings(_env_file=None)
    assert s.encoder_profile == "nvenc"
    assert s.retention_days == 3


def test_readiness_errors_integrated():
    s = Settings(
        mode="integrated",
        database_url="postgresql://u:p@localhost/db",
        storage_backend="minio",
        minio_access_key="ak",
        minio_secret_key="sk",
        _env_file=None,
    )
    assert s.readiness_errors() == []
    s2 = Settings(
        mode="integrated",
        database_url="postgresql://u:p@localhost/db",
        storage_backend="minio",
        minio_access_key="ak",
        minio_secret_key="sk",
        webhook_url="http://hook.example/cb",
        _env_file=None,
    )
    assert any("webhook_secret_ref" in p for p in s2.readiness_errors())


def test_idempotency_key_stable_and_preset_sensitive():
    k1 = compute_idempotency_key("a" * 64, {"job_type": "transcode", "resolution": "720p"})
    k2 = compute_idempotency_key("a" * 64, {"resolution": "720p", "job_type": "transcode"})
    k3 = compute_idempotency_key("a" * 64, {"job_type": "transcode", "resolution": "480p"})
    assert k1 == k2  # canonical JSON: key order does not matter
    assert k1 != k3


# ---------------------------------------------------------------------------
# T2 — encoder profiles + ffmpeg arg building
# ---------------------------------------------------------------------------


def test_resolve_explicit_libx264():
    enc = resolve_encoder("libx264", codec="h264")
    assert enc["profile"] == "libx264"
    assert enc["encoder"] == "libx264"
    assert "-c:v" in enc["video_args"]


def test_resolve_falls_back_to_libx264_when_hw_missing(monkeypatch):
    from maia.video import encoder

    monkeypatch.setattr(encoder, "_available_encoders", lambda *a, **k: set())
    enc = encoder.resolve_encoder("videotoolbox", codec="h264")
    assert enc["profile"] == "libx264"
    assert enc["encoder"] == "libx264"
    # h265 downgrade keeps the h265 CPU codec
    enc265 = encoder.resolve_encoder("nvenc", codec="h265")
    assert enc265["encoder"] == "libx265"


def test_resolve_uses_hw_when_available(monkeypatch):
    from maia.video import encoder

    monkeypatch.setattr(
        encoder, "_available_encoders",
        lambda *a, **k: {"h264_videotoolbox", "hevc_videotoolbox"},
    )
    enc = encoder.resolve_encoder("videotoolbox", codec="h264")
    assert enc["profile"] == "videotoolbox"
    assert enc["encoder"] == "h264_videotoolbox"


def test_build_transcode_args(tmp_path):
    args = build_ffmpeg_args(
        str(tmp_path / "in.mp4"), str(tmp_path / "out.mp4"),
        {"job_type": "transcode", "resolution": "720p", "bitrate_kbps": 2500},
        encoder_profile="libx264",
    )
    joined = " ".join(args)
    assert "-i" in args and str(tmp_path / "in.mp4") in args
    assert "scale=1280:720" in joined
    assert "-b:v" in args and "2500k" in args
    assert args[-1] == str(tmp_path / "out.mp4")


def test_build_hls_args(tmp_path):
    out_dir = tmp_path / "hls"
    args = build_ffmpeg_args(
        str(tmp_path / "in.mp4"), str(out_dir),
        {"job_type": "hls_vod", "resolution": "480p"},
        encoder_profile="libx264",
    )
    joined = " ".join(args)
    assert "-hls_time" in args and "6" in args
    assert "index.m3u8" in joined and "seg_" in joined


def test_build_extract_audio_args():
    args = build_ffmpeg_args("in.mp4", "out.m4a", {"job_type": "extract_audio"})
    assert "-vn" in args and "aac" in args


def test_run_ffmpeg_success_and_failure():
    ok = run_ffmpeg([sys.executable, "-c", "print('ok')"], timeout_sec=30)
    assert ok[-2] == "-c"
    with pytest.raises(PipelineError):
        run_ffmpeg([sys.executable, "-c", "import sys; sys.exit(3)"], timeout_sec=30)


def test_probe_metadata_parses_ffprobe_json(monkeypatch):
    payload = json.dumps({
        "format": {"duration": "12.5"},
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                     "codec_name": "h264"}],
    })

    class P:
        returncode = 0
        stdout = payload

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    meta = probe_metadata("whatever.mp4")
    assert meta["duration_sec"] == 12.5
    assert meta["width"] == 1920 and meta["codec"] == "h264"


def test_probe_metadata_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ffprobe")

    monkeypatch.setattr(subprocess, "run", boom)
    assert probe_metadata("x.mp4") == {}


# ---------------------------------------------------------------------------
# T3 — queue lease/heartbeat/retry/DLQ + worker end-to-end (fake ffmpeg)
# ---------------------------------------------------------------------------


def _fake_run_ok(args, timeout_sec=0):
    """Fake ffmpeg: write a plausible output at the target path."""
    target = Path(args[-1])
    if target.suffix == "":
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.m3u8").write_text("#EXTM3U\n")
        (target / "seg_0000.ts").write_bytes(b"ts")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"encoded-bytes")
    return list(args)


def _fake_probe(path, **kwargs):
    return {"duration_sec": 10.0, "width": 1920, "height": 1080, "codec": "h264"}


def test_lease_claim_and_complete(env):
    job = _enqueue(env)
    q = env.queue
    leased = q.lease_next("w1")
    assert leased is not None and leased.job_id == job.id
    # a second worker gets nothing while the lease is held
    assert q.lease_next("w2") is None
    assert q.complete(leased.job_id, leased.lease,
                      output_uri="out://x", metadata={"duration_sec": 10})
    row = q.get(job.id)
    assert row.status == "done" and row.output_uri == "out://x"


def test_lease_fence_blocks_stale_worker(env):
    job = _enqueue(env)
    q = env.queue
    leased = q.lease_next("w1")
    assert leased is not None
    # a stale/foreign lease token cannot complete the job
    assert q.complete(job.id, leased.lease + 1, output_uri="x", metadata={}) is False


def test_lease_expiry_requeues(env):
    job = _enqueue(env)
    q = env.queue
    leased = q.lease_next("w1")
    assert leased is not None
    # simulate the lease expiring
    with env.factory() as s:
        row = s.get(VideoJob, job.id)
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        s.commit()
    assert q.requeue_expired() == 1
    again = q.lease_next("w2")
    assert again is not None and again.job_id == job.id
    assert again.attempt == 2


def test_worker_retries_then_dlq(env, monkeypatch):
    import maia.video.worker as wmod

    job = _enqueue(env)
    monkeypatch.setattr(wmod, "probe_metadata", _fake_probe)

    def fail_run(args, timeout_sec=0):
        raise PipelineError("boom: bad input")

    monkeypatch.setattr(wmod, "run_ffmpeg", fail_run)
    worker = VideoWorker(env.settings, env.queue, env.storage, worker_id="w-fail")
    assert worker.step() is True  # attempt 1 -> queued (retry)
    assert worker.step() is True  # attempt 2 -> terminal
    assert worker.step() is False  # nothing left to do
    with env.factory() as s:
        row = s.get(VideoJob, job.id)
        assert row.status == "failed" and "boom" in (row.error or "")
        dlq = s.query(VideoJobFailure).all()
        assert len(dlq) == 1 and dlq[0].job_id == job.id


def test_worker_processes_job_end_to_end(env, monkeypatch):
    import maia.video.worker as wmod

    job = _enqueue(env)
    monkeypatch.setattr(wmod, "probe_metadata", _fake_probe)
    monkeypatch.setattr(wmod, "run_ffmpeg", _fake_run_ok)
    notified = []
    worker = VideoWorker(
        env.settings, env.queue, env.storage, worker_id="w-ok",
        notifier=lambda subject, body: notified.append(subject),
    )
    assert worker.step() is True
    with env.factory() as s:
        row = s.get(VideoJob, job.id)
        assert row.status == "done"
        assert row.output_uri and "outputs" in row.output_uri
        assert row.metadata_json["duration_sec"] == 10.0
        assert row.metadata_json["output"]["format"] == "mp4"
    assert notified, "completion notification must fire"


def test_worker_webhook_hmac_signed(env, monkeypatch):
    import hashlib
    import hmac as hmac_mod

    import maia.video.worker as wmod

    _job = _enqueue(env)
    monkeypatch.setattr(wmod, "probe_metadata", _fake_probe)
    monkeypatch.setattr(wmod, "run_ffmpeg", _fake_run_ok)
    calls = []

    def fake_post(url, payload, headers, timeout_sec):
        calls.append((url, payload, headers))

    settings = _settings(
        env.tmp_path,
        webhook_url="http://hook.example/cb",
        webhook_secret_ref="test-secret",
    )
    worker = VideoWorker(settings, env.queue, env.storage, webhook_post=fake_post)
    worker.step()
    assert len(calls) == 1
    _, payload, headers = calls[0]
    expected = hmac_mod.new(b"test-secret", payload, hashlib.sha256).hexdigest()
    assert headers["X-MAIA-Signature"] == f"sha256={expected}"


def test_worker_leases_never_double_process(env, monkeypatch):
    """Two workers cannot claim the same job; a done job is not reprocessed."""
    import maia.video.worker as wmod

    _job = _enqueue(env)
    monkeypatch.setattr(wmod, "probe_metadata", _fake_probe)
    monkeypatch.setattr(wmod, "run_ffmpeg", _fake_run_ok)
    w1 = VideoWorker(env.settings, env.queue, env.storage, worker_id="w1")
    w2 = VideoWorker(env.settings, env.queue, env.storage, worker_id="w2")
    assert w1.step() is True
    assert w2.step() is False  # job already done, nothing queued


# ---------------------------------------------------------------------------
# T4 — API endpoints (standalone app + auth/db dependency overrides)
# ---------------------------------------------------------------------------


class _ApiClient:
    """Thin wrapper: delegates HTTP verbs, exposes `.env` and the FastAPI app."""

    def __init__(self, client: TestClient, app, env) -> None:
        self._client = client
        self._app = app
        self.env = env

    def post(self, *a, **k):
        return self._client.post(*a, **k)

    def get(self, *a, **k):
        return self._client.get(*a, **k)

    def delete(self, *a, **k):
        return self._client.delete(*a, **k)

    @property
    def dependency_overrides(self):
        return self._app.dependency_overrides


@pytest.fixture()
def api(env):
    """Standalone video app with dependency overrides (offline test auth)."""
    from maia.video import api as vap

    app = create_video_app(env.settings)
    app.dependency_overrides[vap.get_video_settings] = lambda: env.settings
    app.dependency_overrides[vap.get_video_user] = lambda: SimpleNamespace(
        tenant_id="tenant-a", email="alice@t-a.test", id=1, role="user",
    )
    with TestClient(app) as client:
        yield _ApiClient(client, app, env)


def _override_tenant(api, tenant: str) -> None:
    from maia.video import api as vap

    api.dependency_overrides[vap.get_video_user] = lambda: SimpleNamespace(
        tenant_id=tenant, email=f"{tenant}@x", id=2, role="user",
    )


def test_api_create_and_get_job(api):
    r = api.post(
        "/video/jobs",
        files={"file": ("clip.mp4", b"fake-bytes", "video/mp4")},
        data={"job_type": "transcode", "resolution": "720p"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["duplicated"] is False
    job_id = body["job"]["job_id"]
    assert body["job"]["status"] == "queued"
    assert body["job"]["preset"]["resolution"] == "720p"

    g = api.get(f"/video/jobs/{job_id}")
    assert g.status_code == 200
    assert g.json()["job_id"] == job_id

    listed = api.get("/video/jobs")
    assert listed.status_code == 200
    assert any(j["job_id"] == job_id for j in listed.json()["jobs"])


def test_api_duplicate_submit_is_idempotent(api):
    payload = {"file": ("same.mp4", b"identical", "video/mp4")}
    data = {"job_type": "transcode", "resolution": "480p"}
    r1 = api.post("/video/jobs", files=payload, data=data)
    r2 = api.post("/video/jobs", files=payload, data=data)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r2.json()["duplicated"] is True
    assert r1.json()["job"]["job_id"] == r2.json()["job"]["job_id"]


def test_api_tenant_isolation(api):
    r = api.post(
        "/video/jobs",
        files={"file": ("clip.mp4", b"tenant-a-bytes", "video/mp4")},
        data={"job_type": "transcode"},
    )
    job_id = r.json()["job"]["job_id"]
    _override_tenant(api, "tenant-b")
    assert api.get(f"/video/jobs/{job_id}").status_code == 404
    listed = api.get("/video/jobs").json()["jobs"]
    assert all(j["tenant_id"] == "tenant-b" for j in listed)


def test_api_validation_errors(api):
    r = api.post(
        "/video/jobs",
        files={"file": ("c.mp4", b"x", "video/mp4")},
        data={"job_type": "teleport"},
    )
    assert r.status_code == 422
    r2 = api.post(
        "/video/jobs",
        files={"file": ("c.mp4", b"x", "video/mp4")},
        data={"job_type": "transcode", "resolution": "5G"},
    )
    assert r2.status_code == 422


def test_api_download_flow(api):
    env = api.env
    r = api.post(
        "/video/jobs",
        files={"file": ("clip.mp4", b"dl-bytes", "video/mp4")},
        data={"job_type": "transcode", "resolution": "720p"},
    )
    job_id = r.json()["job"]["job_id"]
    # not done yet -> 409
    assert api.get(f"/video/jobs/{job_id}/download").status_code == 409
    # simulate completion: write an output file + mark done
    out_path = env.tmp_path / "storage" / "outputs" / job_id / "out.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"encoded")
    with env.factory() as s:
        row = s.get(VideoJob, __import__("uuid").UUID(job_id))
        row.status = "done"
        row.output_uri = str(out_path)
        row.finished_at = datetime.utcnow()
        s.commit()
    resp = api.get(f"/video/jobs/{job_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"encoded"


def test_api_delete_removes_row_and_output(api):
    env = api.env
    r = api.post(
        "/video/jobs",
        files={"file": ("clip.mp4", b"del-bytes", "video/mp4")},
        data={"job_type": "transcode"},
    )
    job_id = r.json()["job"]["job_id"]
    out_path = env.tmp_path / "storage" / "outputs" / job_id / "out.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"x")
    with env.factory() as s:
        row = s.get(VideoJob, __import__("uuid").UUID(job_id))
        row.output_uri = str(out_path)
        s.commit()
    d = api.delete(f"/video/jobs/{job_id}")
    assert d.status_code == 200
    assert api.get(f"/video/jobs/{job_id}").status_code == 404
    assert not out_path.exists()


def test_api_metrics_endpoint(api):
    r = api.get("/video/metrics")
    assert r.status_code == 200
    assert "maia_video_jobs" in r.text


# ---------------------------------------------------------------------------
# Giai doan 4 — retention + metrics
# ---------------------------------------------------------------------------


def test_retention_deletes_old_outputs(env):
    from maia.video.retention import run_retention

    job = _enqueue(env)
    out_path = env.tmp_path / "storage" / "outputs" / str(job.id) / "out.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"old")
    with env.factory() as s:
        row = s.get(VideoJob, job.id)
        row.status = "done"
        row.output_uri = str(out_path)
        row.finished_at = datetime.utcnow() - timedelta(days=10)
        s.commit()

    result = run_retention(env.factory, env.storage, retention_days=7)
    assert result["jobs_deleted"] == 1
    assert not out_path.exists()
    with env.factory() as s:
        assert s.get(VideoJob, job.id) is None


def test_retention_keeps_recent_jobs(env):
    from maia.video.retention import run_retention

    job = _enqueue(env)
    with env.factory() as s:
        row = s.get(VideoJob, job.id)
        row.status = "done"
        row.output_uri = "somewhere"
        row.finished_at = datetime.utcnow() - timedelta(days=1)
        s.commit()
    run_retention(env.factory, env.storage, retention_days=7)
    with env.factory() as s:
        assert s.get(VideoJob, job.id) is not None


def test_metrics_registry_and_render():
    vmetrics.incr_counter("maia_video_jobs_completed_total", tenant_id="t1")
    vmetrics.observe("maia_video_encode_seconds", 1.2)
    text = vmetrics.render_metrics()
    assert "maia_video_jobs_completed_total" in text
    assert "maia_video_encode_seconds_bucket" in text
    assert "maia_video_encode_seconds_sum" in text


def test_update_queue_gauges_reflects_state(env):
    from maia.video.retention import update_queue_gauges

    _enqueue(env)
    counts = update_queue_gauges(env.queue)
    assert counts.get("queued", 0) >= 1
    snap = vmetrics.snapshot()
    assert any(k.startswith("maia_video_jobs{") for k in snap["gauges"])


# ---------------------------------------------------------------------------
# T5 — Alembic migration (offline SQL generation, no DB required)
# ---------------------------------------------------------------------------


def test_alembic_offline_sql_creates_video_tables():
    """`alembic upgrade head --sql` must emit the vid_jobs DDL without a DB."""
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, VID_DATABASE_URL="postgresql+psycopg://u:p@localhost/db")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c",
         str(repo_root / "alembic_video.ini"), "upgrade", "head", "--sql"],
        cwd=repo_root, capture_output=True, text=True, env=env, timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    sql = proc.stdout
    assert "CREATE TABLE vid_jobs" in sql
    assert "CREATE TABLE vid_jobs_failed" in sql
    assert "uq_vid_jobs_idempotency_key" in sql


# ---------------------------------------------------------------------------
# Real-ffmpeg integration (skipped when ffmpeg is not installed)
# ---------------------------------------------------------------------------

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


@requires_ffmpeg
def test_real_ffmpeg_transcode_end_to_end(env):
    """Full pipeline with the REAL ffmpeg: generate a 1s test clip, transcode
    via libx264, verify metadata + output file."""
    src = env.tmp_path / "real_in.mp4"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=24",
            "-f", "lavfi", "-i", "sine=duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(src),
        ],
        check=True, capture_output=True,
    )
    job = _enqueue(env, source=src)
    worker = VideoWorker(env.settings, env.queue, env.storage, worker_id="w-real")
    assert worker.step() is True
    row = env.queue.get(job.id)
    assert row.status == "done", row.error
    out = Path(row.output_uri)
    assert out.exists() and out.stat().st_size > 0
    assert row.metadata_json["width"] == 640
    assert row.metadata_json["duration_sec"] == 1.0




