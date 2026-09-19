"""Video pipeline settings — VID_-prefixed, standalone (T1).

Follows the Service Desk settings convention (``maia.servicedesk.settings``):
- flat fields, env-bound via ``VID_`` prefix, ``.env.video`` file.
- ``mode`` gates offline (dev/test/demo) vs integrated (production) behaviour.
- integrated mode REQUIRES a PostgreSQL database_url and never silently
  degrades (no mock fallbacks — Global Constraints).
- ``storage_backend`` selects MinIO (S3-compatible) or the legacy local
  ``STORAGE_DIR``-style folder for offline/dev use.
"""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_POSTGRES_SCHEMES = ("postgresql://", "postgresql+psycopg://")


class Settings(BaseSettings):
    """Flat, VID_-prefixed settings for the video pipeline deployment."""

    model_config = SettingsConfigDict(
        env_prefix="VID_",
        env_file=".env.video",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Mode gates (offline = tests/dev; integrated = production).
    mode: Literal["offline", "integrated"] = "offline"
    database_url: str = ""
    tenant_id: str = "default"

    # Encoder profile. ``auto`` probes the platform ffmpeg at first use and
    # picks videotoolbox (macOS host) / nvenc (Linux+NVIDIA) / libx264 (CPU,
    # works everywhere incl. Linux containers). Explicit values win.
    encoder_profile: Literal["auto", "videotoolbox", "nvenc", "libx264"] = "auto"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    ffmpeg_timeout_sec: float = 3600.0

    # Storage backend.
    storage_backend: Literal["minio", "local"] = "local"
    storage_dir: str = "./storage/video"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket_in: str = "maia-video-in"
    minio_bucket_out: str = "maia-video-out"
    minio_secure: bool = False

    # Worker tuning (lease/heartbeat pattern mirrors Service Desk S4/S7).
    worker_concurrency: int = 1
    worker_id: str = ""
    lease_seconds: int = 300
    heartbeat_seconds: int = 15
    max_attempts: int = 3
    poll_interval_sec: float = 2.0

    # Completion webhook (HMAC-signed when secret configured).
    webhook_url: str = ""
    webhook_secret_ref: str = ""

    # Retention: delete outputs + temp files older than N days.
    retention_days: int = 7

    # Upload size cap (MB) enforced while streaming POST /video/jobs.
    max_upload_mb: int = 2048

    @model_validator(mode="after")
    def _enforce_mode_gates(self) -> Settings:
        if self.mode == "integrated":
            if not self.database_url.startswith(_POSTGRES_SCHEMES):
                raise ValueError(
                    "integrated mode requires a PostgreSQL database_url "
                    "(postgresql:// or postgresql+psycopg://), got: "
                    f"{self.database_url or '<empty>'!r}"
                )
            if self.storage_backend == "local":
                raise ValueError(
                    "integrated mode requires storage_backend='minio': local "
                    "folder storage is an offline/dev convenience only"
                )
        return self

    def readiness_errors(self) -> list[str]:
        """Human-readable config gaps that block integrated readiness."""
        problems: list[str] = []
        if not self.database_url.startswith(_POSTGRES_SCHEMES):
            problems.append("database_url must be PostgreSQL (all modes)")
        if self.mode == "integrated":
            if not self.minio_access_key or not self.minio_secret_key:
                problems.append(
                    "minio_access_key/minio_secret_key are required in "
                    "integrated mode"
                )
            if self.webhook_url and not self.webhook_secret_ref:
                problems.append(
                    "webhook_secret_ref is required when webhook_url is set "
                    "(secrets by reference, never inlined)"
                )
        return problems


def settings_from_env() -> Settings:
    """Load Settings from .env.video / VID_* environment."""
    return Settings()
