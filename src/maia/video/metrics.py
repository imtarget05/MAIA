"""Minimal Prometheus-text metrics for the video pipeline (Giai doan 4).

No third-party dependency: counters/gauges/histograms are held in a
module-level registry and rendered in the Prometheus exposition format via
``render_metrics()`` (served on ``GET /video/metrics``).

Custom metrics (plan S4.1):
- maia_video_jobs_queued / leased / done / failed   (gauges)
- maia_video_jobs_completed_total{tenant}            (counter)
- maia_video_jobs_failed_total{tenant}               (counter)
- maia_video_encode_seconds (histogram: full job wall time)
- maia_video_processed_source_seconds_total          (counter)
- maia_video_worker_active{worker_id}                (gauge)
"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_gauges: dict[str, float] = {}
_counters: dict[str, float] = {}
_histograms: dict[str, dict[str, float]] = {}
HIST_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 300, 900, 3600)


def _key(name: str, labels: dict[str, Any] | None) -> str:
    if not labels:
        return name
    parts = ",".join(
        f'{k}="{str(v).replace(chr(92), "").replace(chr(34), "")}"'
        for k, v in sorted(labels.items())
    )
    return f"{name}{{{parts}}}"


def set_gauge(name: str, value: float, **labels: Any) -> None:
    with _lock:
        _gauges[_key(name, labels or None)] = float(value)


def incr_counter(name: str, value: float = 1.0, **labels: Any) -> None:
    with _lock:
        k = _key(name, labels or None)
        _counters[k] = _counters.get(k, 0.0) + float(value)


def observe(name: str, value: float, **labels: Any) -> None:
    with _lock:
        k = _key(name, labels or None)
        h = _histograms.setdefault(
            k, {"count": 0.0, "sum": 0.0, **{f"bucket_{b}": 0.0 for b in HIST_BUCKETS}}
        )
        h["count"] += 1.0
        h["sum"] += float(value)
        for b in HIST_BUCKETS:
            if value <= b:
                h[f"bucket_{b}"] += 1.0


def snapshot() -> dict[str, dict[str, dict[str, float]]]:
    """Read-only copy for tests.

    Returns a nested view: ``{"gauges": {name: {"value": v}}, ...}`` so the
    histogram dicts (bucket_N/sum/count) and scalar series share one shape.
    """
    with _lock:
        gauges = {k: {"value": float(v)} for k, v in _gauges.items()}
        counters = {k: {"value": float(v)} for k, v in _counters.items()}
        histograms = {k: dict(v) for k, v in _histograms.items()}
    return {"gauges": gauges, "counters": counters, "histograms": histograms}


def render_metrics() -> str:
    """Prometheus exposition format (text/plain; version 0.0.4)."""
    lines: list[str] = []
    with _lock:
        seen_types: set[str] = set()
        for name, value in sorted(_gauges.items()):
            base = name.split("{")[0]
            if base not in seen_types:
                lines.append(f"# TYPE {base} gauge")
                seen_types.add(base)
            lines.append(f"{name} {value}")
        for name, value in sorted(_counters.items()):
            base = name.split("{")[0]
            if base not in seen_types:
                lines.append(f"# TYPE {base} counter")
                seen_types.add(base)
            lines.append(f"{name} {value}")
        for name, h in sorted(_histograms.items()):
            base = name.split("{")[0]
            labels = name.split("{", 1)[1].rstrip("}") if "{" in name else ""
            lines.append(f"# TYPE {base} histogram")
            for b in HIST_BUCKETS:
                le = f'{base}_bucket{{{labels},le="{b}"}}' if labels else f'{base}_bucket{{le="{b}"}}'
                lines.append(f"{le} {h[f'bucket_{b}']}")
            lines.append(f"{_suffix(name, base, labels, '_sum')} {h['sum']}")
            lines.append(f"{_suffix(name, base, labels, '_count')} {h['count']}")
    return "\n".join(lines) + "\n"


def _suffix(name: str, base: str, labels: str, suffix: str) -> str:
    return f"{base}{suffix}{{{labels}}}" if labels else f"{base}{suffix}"
