"""FFmpeg pipeline builder (T2) — pure builders + subprocess execution.

Storage (local folder / MinIO) is isolated in ``storage.py``.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maia.video.encoder import resolve_encoder
from maia.video.schemas import compute_idempotency_key  # re-export convenience

__all__ = [
    "PipelineError",
    "PipelineResult",
    "build_ffmpeg_args",
    "compute_idempotency_key",
    "probe_metadata",
    "run_ffmpeg",
]


class PipelineError(RuntimeError):
    """Raised when the ffmpeg/ffprobe subprocess fails or times out."""


@dataclass
class PipelineResult:
    """Outcome of one executed pipeline run."""

    output_path: str
    duration_sec: float | None = None
    command: list[str] | None = None


# Resolution name -> (width, height)
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (854, 480),
    "360p": (640, 360),
}


def build_ffmpeg_args(
    input_path: str,
    output_path: str,
    preset: dict[str, Any],
    *,
    encoder_profile: str = "auto",
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Build the ffmpeg argument list for one job. Pure — no side effects."""
    job_type = preset.get("job_type", "transcode")
    codec = preset.get("codec", "h264")
    enc = resolve_encoder(encoder_profile, codec=codec, ffmpeg_bin=ffmpeg_bin)

    args: list[str] = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        *enc["hwaccel"],
        "-i", input_path,
    ]
    if job_type == "extract_audio":
        args += ["-vn", "-c:a", "aac", "-b:a", "128k"]
    elif job_type == "remux":
        # Stream-copy: no re-encode, container change only.
        args += ["-c", "copy"]
    elif job_type == "hls_vod":
        args += [
            *enc["video_args"],
            "-c:a", "aac",
            "-hls_time", "6",
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", os.path.join(output_path, "seg_%04d.ts"),
            os.path.join(output_path, "index.m3u8"),
        ]
    else:  # transcode (default)
        args += [*enc["video_args"], "-c:a", "aac"]
        res = preset.get("resolution")
        if res and res in RESOLUTIONS:
            w, h = RESOLUTIONS[res]
            args += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease"]
        if preset.get("bitrate_kbps"):
            args += ["-b:v", f"{int(preset['bitrate_kbps'])}k"]
    if job_type != "hls_vod":  # file-backed outputs take the output path last
        args += [output_path]
    return args


def _kill_tree(pid: int) -> None:
    """Best-effort process-tree kill (POSIX: own process group)."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def run_ffmpeg(
    args: list[str],
    *,
    timeout_sec: float = 3600.0,
) -> list[str]:
    """Run the ffmpeg subprocess with timeout + process-group kill.

    Returns the executed command (for audit), raises ``PipelineError`` on
    failure. The child runs in its own process group so a timeout kills
    ffmpeg and any grandchildren.
    """
    executed = list(args)
    try:
        proc = subprocess.Popen(
            executed,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group (POSIX)
        )
    except OSError as exc:
        raise PipelineError(f"failed to start ffmpeg: {exc}") from exc
    try:
        _, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        proc.wait(timeout=10)
        raise PipelineError(
            f"ffmpeg timed out after {timeout_sec}s: {' '.join(executed[:6])}..."
        )
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detail = tail[-1] if tail else "no stderr"
        raise PipelineError(f"ffmpeg exited {proc.returncode}: {detail[:500]}")
    return executed


def probe_metadata(
    media_path: str,
    *,
    ffprobe_bin: str = "ffprobe",
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """ffprobe a media file: duration / resolution / codec (best-effort).

    Returns ``{}`` (never raises) when ffprobe is missing or the file is not
    media — metadata is informational, it must not fail the job.
    """
    import json

    cmd = [
        ffprobe_bin, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(media_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    fmt = data.get("format", {})
    vstream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    return {
        "duration_sec": float(fmt["duration"]) if fmt.get("duration") else None,
        "width": vstream.get("width"),
        "height": vstream.get("height"),
        "codec": vstream.get("codec_name"),
    }


def make_temp_workspace(job_id: str) -> Path:
    """Temp dir for one job: ``<tmpdir>/vid_<job_id>/``."""
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")[:64] or "job"
    p = Path(tempfile.gettempdir()) / f"vid_{safe}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cleanup_workspace(workspace: Path) -> None:
    """Remove a job's temp workspace tree (best-effort)."""
    import shutil

    shutil.rmtree(workspace, ignore_errors=True)


