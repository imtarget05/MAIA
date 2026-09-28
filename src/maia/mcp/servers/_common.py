"""Shared helpers for the MCP servers (durable local state, ids, soft failures).

Kept in one module because every integration server needs the same three things
and a second copy of them would drift:

* **Durable local state** instead of "pretend it worked": when a credential is
  absent the server writes to a local store and reports ``dry_run: true``. The
  payload shape is identical to the remote path, so a caller (and its tests) does
  not need two code paths.
* **Stable delivery/record ids** derived from content, so a retried call is
  idempotent instead of creating a duplicate row or a duplicate email.
* **Path helpers that respect ``settings``** but accept an override, which is
  what makes the servers testable without touching the real storage directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...common import content_id, now_iso, stable_key

__all__ = [
    "append_jsonl",
    "content_id",
    "now_iso",
    "read_json_file",
    "stable_key",
    "write_json_file",
]

# NOTE: the id/timestamp primitives live in ``maia.common`` so the ingestion
# pipeline (which must not depend on the MCP layer) uses the same semantics.
# They are re-exported here because the MCP servers' import surface is written
# against this module.


def read_json_file(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupt local store: start clean rather than crash the tool call. The
        # caller records the reset in its result payload.
        return default



def write_json_file(path: str | Path, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)  # atomic: a crash mid-write must not truncate the store


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
