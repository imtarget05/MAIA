"""Video worker (T3) — poll loop: lease -> fetch -> ffmpeg -> upload -> commit.

Runs as its own process (never inside the web process). Every step is
idempotent keyed by ``job_id`` so an at-least-once lease replay never
double-produces output. On completion it fires the HMAC webhook (if
configured) and the MAIA email notifier (re-used, never raises).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from maia.video import metrics as _metrics
from maia.video.pipeline import (
    build_ffmpeg_args,
    cleanup_workspace,
    make_temp_workspace,
    probe_metadata,
    run_ffmpeg,
)
from maia.video.queue import Queue
from maia.video.settings import Settings
from maia.video.storage import Storage, build_storage

logger = logging.getLogger("maia.video.worker")


class VideoWorker:
    """Single-threaded worker loop; run N processes for N-way parallelism."""

    def __init__(
        self,
        settings: Settings,
        queue: Queue,
        storage: Storage,
        *,
        worker_id: str | None = None,
        notifier=None,
        webhook_post=None,
        clock=None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.storage = storage
        self.worker_id = (
            worker_id or settings.worker_id or f"vid-worker-{uuid.uuid4().hex[:8]}"
        )
        self._notifier = notifier
        self._webhook_post = webhook_post or self._default_webhook_post
        self._clock = clock or time.time  # injectable for retention timing
        self._stop = False

    # -- main loop -----------------------------------------------------------

    def run_forever(self, *, poll_interval_sec: float | None = None) -> None:
        interval = poll_interval_sec or self.settings.poll_interval_sec
        last_retention = 0.0
        while not self._stop:
            try:
                worked = self.step()
            except Exception:  # resilience: loop must never die
                logger.exception("video worker step crashed")
                worked = False
            if not worked:
                # idle pass: recover expired leases, refresh gauges, sleep
                try:
                    self.queue.requeue_expired()
                except Exception:
                    logger.exception("requeue_expired failed")
                try:
                    from maia.video.retention import update_queue_gauges
                    update_queue_gauges(self.queue)
                except Exception:
                    logger.exception("update_queue_gauges failed")
                # hourly retention sweep (S4.3)
                now_ts = self._clock()
                if now_ts - last_retention >= 3600.0:
                    last_retention = now_ts
                    try:
                        from maia.video.retention import run_retention
                        run_retention(
                            self.queue.factory, self.storage,
                            retention_days=self.settings.retention_days,
                        )
                    except Exception:
                        logger.exception("retention sweep failed")
                time.sleep(interval)

    def stop(self) -> None:
        self._stop = True

    # -- one unit of work ----------------------------------------------------

    def step(self) -> bool:
        """Process at most one job. Returns True when a job was processed."""
        leased = self.queue.lease_next(self.worker_id)
        if leased is None:
            return False
        job_id_str = str(leased.job_id)
        workspace = make_temp_workspace(job_id_str)
        started = time.time()
        try:
            result = self._process(leased, workspace)
            done = self.queue.complete(
                leased.job_id,
                leased.lease,
                output_uri=result["output_uri"],
                metadata=result["metadata"],
            )
            if done:
                _metrics.incr_counter(
                    "maia_video_jobs_completed_total",
                    tenant_id=leased.tenant_id,
                )
                _metrics.observe("maia_video_encode_seconds", time.time() - started)
                src_sec = (result["metadata"] or {}).get("duration_sec") or 0
                _metrics.incr_counter(
                    "maia_video_processed_source_seconds_total", value=float(src_sec)
                )
                self._notify_completion(leased, result)
            return done
        except Exception as exc:
            new_status = self.queue.fail(leased.job_id, leased.lease, str(exc)[:2000])
            if new_status == "failed":
                _metrics.incr_counter("maia_video_jobs_failed_total")
            logger.warning("job %s failed (attempt %s -> %s): %s",
                           job_id_str, leased.attempt, new_status, exc)
            return True  # we did process (a failure) this pass
        finally:
            cleanup_workspace(workspace)

    # -- pipeline ------------------------------------------------------------

    def _process(self, leased: Any, workspace: Path) -> dict[str, Any]:
        s = self.settings
        job_id_str = str(leased.job_id)
        # 1) fetch source
        local_src = workspace / "input"
        self.storage.download(leased.source_uri, local_src)
        # 2) probe metadata (informational, never fatal)
        meta = probe_metadata(str(local_src), ffprobe_bin=s.ffprobe_bin)
        meta["source"] = {"uri": leased.source_uri}
        # 3) build + run ffmpeg
        preset = leased.preset or {}
        job_type = preset.get("job_type", "transcode")
        fmt = preset.get("format", "mp4")
        if job_type == "hls_vod" or fmt == "hls":
            output_target: str | Path = workspace / "hls"
            output_target.mkdir(parents=True, exist_ok=True)
        else:
            ext = "m4a" if job_type == "extract_audio" else "mp4"
            output_target = workspace / f"output.{ext}"
        args = build_ffmpeg_args(
            str(local_src), str(output_target), preset,
            encoder_profile=s.encoder_profile, ffmpeg_bin=s.ffmpeg_bin,
        )
        run_ffmpeg(args, timeout_sec=s.ffmpeg_timeout_sec)
        # 4) upload output (idempotent key: job_id)
        key = f"outputs/{job_id_str}"
        if output_target.is_dir():
            output_uri = self.storage.upload(str(output_target), key)
        else:
            suffix = ".m4a" if job_type == "extract_audio" else ".mp4"
            output_uri = self.storage.upload(str(output_target), f"{key}{suffix}")
        meta["output"] = {"uri": output_uri, "job_type": job_type, "format": fmt}
        return {"output_uri": output_uri, "metadata": meta}

    # -- completion side effects ----------------------------------------------

    def _notify_completion(self, leased: Any, result: dict[str, Any]) -> None:
        # webhook (HMAC-signed when secret configured)
        try:
            self._fire_webhook(leased, result)
        except Exception:
            logger.exception("webhook failed (job still done)")
        # MAIA email notifier — re-used, never raises
        if self._notifier is not None:
            try:
                self._notifier(
                    subject=f"[MAIA] Video job {leased.job_id} hoan thanh",
                    body=(f"Job: {leased.job_id}\nOutput: {result['output_uri']}\n"
                          f"Attempt: {leased.attempt}"),
                )
            except Exception:
                logger.exception("notifier failed (job still done)")

    def _fire_webhook(self, leased: Any, result: dict[str, Any]) -> None:
        url = self.settings.webhook_url
        if not url:
            return
        payload = json.dumps({
            "event": "video.job.completed",
            "job_id": str(leased.job_id),
            "output_uri": result["output_uri"],
            "metadata": result["metadata"],
            "ts": datetime.utcnow().isoformat() + "Z",
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = self._webhook_secret()
        if secret:
            sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
            headers["X-MAIA-Signature"] = f"sha256={sig}"
        self._webhook_post(url, payload, headers, timeout_sec=10.0)

    def _webhook_secret(self) -> bytes | None:
        ref = self.settings.webhook_secret_ref
        if not ref:
            return None
        # secrets by reference: "env:VAR_NAME" or a raw secret for dev only
        if ref.startswith("env:"):
            import os
            val = os.environ.get(ref[4:], "")
            return val.encode("utf-8") if val else None
        return ref.encode("utf-8")

    @staticmethod
    def _default_webhook_post(url: str, payload: bytes,
                              headers: dict[str, str], timeout_sec: float) -> None:
        import requests

        requests.post(url, data=payload, headers=headers, timeout=timeout_sec)


def build_worker(settings: Settings, factory: sessionmaker) -> VideoWorker:
    """Wire a worker from Settings + a session factory (compose entrypoint)."""
    queue = Queue(
        factory,
        lease_seconds=settings.lease_seconds,
        max_attempts=settings.max_attempts,
    )
    storage = build_storage(settings)
    return VideoWorker(settings, queue, storage)


def main() -> None:
    """Entrypoint: ``python -m maia.video.worker [concurrency]``."""
    import sys

    from maia.video.db import make_engine, make_session_factory, resolve_database_url

    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    url = resolve_database_url(settings)
    factory = make_session_factory(make_engine(url))
    workers = [build_worker(settings, factory) for _ in range(max(concurrency, 1))]
    logger.info("starting %d video worker(s) as %s", len(workers),
                [w.worker_id for w in workers])
    try:
        for w in workers:
            w.run_forever()  # single-process mode: first worker blocks
    except KeyboardInterrupt:
        for w in workers:
            w.stop()


if __name__ == "__main__":
    main()


