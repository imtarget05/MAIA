"""Outbox background worker — self-contained lifespan loop.

The manual path stays ``drain_outbox()`` (used by the admin endpoint and by
tests, which must not wait on a timer). This module adds the automatic path:

- :func:`scheduler_tick` — one drain pass with retry bookkeeping: every
  entry carries ``attempts``; SMTP failures increment it (kept queued);
  entries past ``OUTBOX_MAX_RETRIES`` move to ``outbox_deadletter.json``.
- :func:`run_forever` — polling loop for the FastAPI lifespan hook.
- :func:`start_scheduler` / :func:`stop_scheduler` — lifecycle used by
  ``api.py``; NO-OP unless ``OUTBOX_WORKER_ENABLED=true`` so unit tests
  stay deterministic (no stray threads).

Simulated success (no SMTP_HOST) never counts as a failure: ``drain_outbox``
stamps ``dispatched`` and those entries leave the queue on first pass.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

from .config import settings

log = logging.getLogger("maia.outbox_worker")

_DEADLETTER_FILENAME = "outbox_deadletter.json"

_scheduler_thread: threading.Thread | None = None
_scheduler_stop: threading.Event = threading.Event()


def _deadletter_path() -> Path:
    from .outbox_worker import _outbox_path

    p = _outbox_path().parent / _DEADLETTER_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_deadletter() -> list[dict]:
    p = _deadletter_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_deadletter(items: list) -> None:
    tmp = _deadletter_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_deadletter_path())


def read_deadletter(limit: int = 100) -> list[dict]:
    return _load_deadletter()[-limit:][::-1]


def scheduler_tick(*, limit: int = 500) -> dict:
    """One automatic drain pass with retry bookkeeping.

    Reuses :func:`outbox_worker.drain_outbox` for the actual send, then
    bumps ``attempts`` on whatever is still queued (SMTP failures).
    Entries hitting ``OUTBOX_MAX_RETRIES`` move to dead-letter storage.
    Returns {"dispatched", "pending", "dead_lettered"}.
    """
    from . import notifier as _nt
    from .outbox_worker import _load_json_list, _write_json_list, drain_outbox

    max_retries = max(1, int(settings.OUTBOX_MAX_RETRIES or 5))
    if not _load_json_list(_nt._outbox_path()):
        return {"dispatched": 0, "pending": 0, "dead_lettered": 0}
    result = drain_outbox(limit=limit)
    still_queued = _load_json_list(_nt._outbox_path())
    dead = _load_deadletter()
    dead_lettered = 0
    remaining: list[dict] = []
    for entry in still_queued:
        if not isinstance(entry, dict):
            continue
        attempts = int(entry.get("attempts", 0) or 0) + 1
        entry["attempts"] = attempts
        if attempts >= max_retries:
            entry["dead_lettered"] = True
            dead.append(entry)
            dead_lettered += 1
        else:
            remaining.append(entry)
    _write_json_list(_nt._outbox_path(), remaining)
    if dead_lettered:
        _write_deadletter(dead)
    return {"dispatched": int(result.get("dispatched", 0) or 0),
            "pending": len(remaining), "dead_lettered": dead_lettered}


async def run_forever(*, interval_sec: float | None = None,
                      stop: threading.Event | None = None) -> None:
    """Polling loop — one scheduler_tick per interval until stopped."""
    delay = float(interval_sec if interval_sec is not None
                  else settings.OUTBOX_WORKER_INTERVAL_SEC or 60.0)
    stop_evt = stop if stop is not None else _scheduler_stop
    while not stop_evt.is_set():
        try:
            await asyncio.to_thread(scheduler_tick)
        except Exception as e:  # never kill the loop on one bad pass
            log.warning("outbox scheduler tick failed: %s: %s",
                        type(e).__name__, e)
        stop_evt.wait(delay)


def _thread_main() -> None:
    try:
        asyncio.run(run_forever())
    except Exception as e:  # pragma: no cover — defensive
        log.warning("outbox scheduler stopped: %s: %s", type(e).__name__, e)


def start_scheduler() -> bool:
    """Start the background thread. NO-OP unless OUTBOX_WORKER_ENABLED.

    Returns True when a thread is (or already is) running.
    """
    global _scheduler_thread
    if not settings.OUTBOX_WORKER_ENABLED:
        return False
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return True
    _scheduler_stop.clear()
    if settings.OUTBOX_WORKER_STARTUP_DRAIN:
        try:
            scheduler_tick()
        except Exception as e:
            log.warning("outbox startup drain failed: %s: %s",
                        type(e).__name__, e)
    _scheduler_thread = threading.Thread(target=_thread_main, name="maia-outbox",
                                         daemon=True)
    _scheduler_thread.start()
    return True


def stop_scheduler(*, timeout: float = 5.0) -> None:
    """Signal the loop to stop and wait briefly (graceful shutdown)."""
    global _scheduler_thread
    _scheduler_stop.set()
    t, _scheduler_thread = _scheduler_thread, None
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=timeout)


def scheduler_status() -> dict:
    return {"enabled": bool(settings.OUTBOX_WORKER_ENABLED),
            "running": bool(_scheduler_thread is not None
                            and _scheduler_thread.is_alive()),
            "interval_sec": float(settings.OUTBOX_WORKER_INTERVAL_SEC or 60.0),
            "max_retries": int(settings.OUTBOX_MAX_RETRIES or 5)}

