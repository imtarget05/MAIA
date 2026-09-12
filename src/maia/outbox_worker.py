"""Outbox worker — dispatch queued notifications from ``storage/outbox.json``.

When SMTP is not configured (the default), :mod:`maia.notifier` only queues
emails. :func:`drain_outbox` processes every entry that has not been
dispatched yet: with SMTP configured it attempts a real send, otherwise it
simulates a successful send. Every dispatched entry is stamped with
``dispatched: true`` + ``dispatched_at`` and appended to
``storage/outbox_history.json``; dispatched entries are removed from the live
queue so the outbox is left clean.
"""
from __future__ import annotations

import json
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from .config import settings

_OUTBOX_FILENAME = "outbox.json"
_HISTORY_FILENAME = "outbox_history.json"


def _outbox_path() -> Path:
    p = Path(settings.STORAGE_DIR) / _OUTBOX_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _history_path() -> Path:
    p = Path(settings.STORAGE_DIR) / _HISTORY_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [e for e in data] if isinstance(data, list) else []


def _write_json_list(path: Path, items: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _smtp_send(entry: dict) -> None:
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = entry.get("to", "")
    msg["Subject"] = entry.get("subject", "")
    msg["Date"] = formatdate(localtime=False)
    msg.set_content(entry.get("body", ""))
    if settings.SMTP_USE_TLS:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
        server.starttls()
    else:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
    try:
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def drain_outbox(*, limit: int = 500) -> dict:
    """Dispatch every undispatched entry in the outbox queue.

    Returns ``{"dispatched": <int>, "pending": <int>}`` where ``dispatched``
    counts entries successfully sent in this call and ``pending`` counts
    entries still waiting (SMTP failures are kept queued for retry).
    """
    outbox = _load_json_list(_outbox_path())
    if not outbox:
        return {"dispatched": 0, "pending": 0}

    history = _load_json_list(_history_path())
    dispatched = 0
    remaining: list[dict] = []
    for entry in outbox:
        if not isinstance(entry, dict):
            continue
        if entry.get("dispatched") is True:
            history.append(entry)
            continue
        if dispatched >= limit:
            remaining.append(entry)
            continue
        if settings.SMTP_HOST:
            try:
                _smtp_send(entry)
                entry = {**entry, "via": "smtp"}
            except Exception as e:
                entry = {**entry, "smtp_error": f"{type(e).__name__}: {e}"}
                remaining.append(entry)
                continue
        else:
            # No SMTP configured: simulated successful send.
            entry = {**entry, "via": entry.get("via") or "outbox", "simulated": True}
        stamp = _utc_now_iso()
        entry = {**entry, "dispatched": True, "dispatched_at": stamp}
        history.append(entry)
        dispatched += 1

    _write_json_list(_history_path(), history)
    _write_json_list(_outbox_path(), remaining)
    return {"dispatched": dispatched, "pending": len(remaining)}
