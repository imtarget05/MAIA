"""Encoder profile resolution for the video pipeline (T2).

Selects the ffmpeg video codec arguments per platform:

- ``videotoolbox`` — macOS host HW (h264_videotoolbox / hevc_videotoolbox).
  NOT available inside Linux containers (Docker on macOS runs a Linux VM).
- ``nvenc``        — Linux + NVIDIA GPU (h264_nvenc / hevc_nvenc).
- ``libx264``      — pure CPU, works everywhere; the mandatory fallback.

``auto`` probes ``ffmpeg -hide_banner -encoders`` once and caches the choice.
An explicit ``VID_ENCODER_PROFILE`` always wins; if the requested HW encoder
is unavailable the resolver degrades to libx264 (never fails the job at
argument-build time — ffmpeg failing at runtime is handled by the worker).
"""
from __future__ import annotations

import shutil
import subprocess
import threading

# profile -> (h264 encoder, h265 encoder)
_PROFILES: dict[str, tuple[str, str]] = {
    "videotoolbox": ("h264_videotoolbox", "hevc_videotoolbox"),
    "nvenc": ("h264_nvenc", "hevc_nvenc"),
    "libx264": ("libx264", "libx265"),
}

_lock = threading.Lock()
_cached_auto: str | None = None


def _available_encoders(ffmpeg_bin: str) -> set[str]:
    """Parse ``ffmpeg -encoders`` output into a set of encoder names."""
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        # encoder lines look like: " V....D h264_videotoolbox  VideoToolbox ..."
        parts = line.split(maxsplit=2)
        if len(parts) >= 2:
            names.add(parts[1].strip())
    return names


def probe_auto_profile(ffmpeg_bin: str = "ffmpeg") -> str:
    """Probe once per process: videotoolbox > nvenc > libx264."""
    global _cached_auto
    with _lock:
        if _cached_auto is not None:
            return _cached_auto
        have = _available_encoders(ffmpeg_bin)
        for candidate, h264_name in (
            ("videotoolbox", "h264_videotoolbox"),
            ("nvenc", "h264_nvenc"),
            ("libx264", "libx264"),
        ):
            if h264_name in have:
                _cached_auto = candidate
                return candidate
        _cached_auto = "libx264"
        return _cached_auto


def resolve_encoder(
    profile: str,
    *,
    codec: str = "h264",
    ffmpeg_bin: str = "ffmpeg",
) -> dict:
    """Return ``{"profile", "encoder", "video_args", "hwaccel"}`` for a job.

    ``codec`` is the target codec family: "h264" or "h265".
    """
    if profile == "auto":
        profile = probe_auto_profile(ffmpeg_bin)
    profile = profile if profile in _PROFILES else "libx264"

    h264_name, h265_name = _PROFILES[profile]
    encoder = h265_name if codec == "h265" else h264_name

    available = _available_encoders(ffmpeg_bin) if profile != "libx264" else set()
    if profile != "libx264" and encoder not in available:
        # Requested HW encoder missing (e.g. videotoolbox inside a Linux
        # container) — degrade to CPU instead of failing the build.
        profile = "libx264"
        encoder = "libx265" if codec == "h265" else "libx264"

    video_args: list[str] = []
    hwaccel: list[str] = []
    if profile == "videotoolbox":
        # VideoToolbox likes explicit bitrate; -q:v unused.
        video_args = ["-c:v", encoder]
    elif profile == "nvenc":
        video_args = ["-c:v", encoder, "-preset", "p4"]
        hwaccel = ["-hwaccel", "cuda"]
    else:
        # CPU: preset tuned for reasonable speed/quality trade-off.
        video_args = ["-c:v", encoder, "-preset", "veryfast"]
    return {
        "profile": profile,
        "encoder": encoder,
        "video_args": video_args,
        "hwaccel": hwaccel,
    }


def ffmpeg_available(ffmpeg_bin: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg_bin) is not None
