"""SSE token streaming contract for POST /chat/stream (§9 realtime closeout).

Event contract (typed, explicit — no bare ``data:`` frames):

    event: meta               {"trace_id", "mode", "tenant_id"}
    event: token              {"delta", "seq"}
    event: citations          {"sources": [{"chunk_id", "document", "tag"}]}
    event: approval_required  {"tool", "summary", "session_id"}
    event: error              {"code", "recoverable"}   (no stack traces)
    event: done               {"finish_reason", "latency_ms", ...}

Guarantees the endpoint relies on:

* ``done`` is emitted exactly once per stream (guard flag).
* Assistant content is persisted only when the stream completes — a client
  disconnect discards the buffered session writes, so a partial response is
  never stored as a completed answer (STREAM-010). Pending HIGH_RISK
  proposals (``set_pending``, a different store) are NOT buffered: an
  ``approval_required`` pause must survive the stream ending.
* HIGH_RISK tools never execute on the stream path: streaming only renders
  the already-grounded ``agent.chat()`` result. Execution stays in
  ``confirm_action()`` (C1). This module performs no tool calls at all.
* No background tasks are created: generation runs in the request's async
  task (``asyncio.to_thread``); on disconnect the result is dropped and the
  buffered writes discarded. A sync ``agent.chat()`` in flight cannot be
  force-cancelled mid-LLM-call — documented limitation, not a leak.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

# Stream-level timeout (seconds) bounding retrieval+generation before the
# first token. Provider LLM timeout (settings.LLM_TIMEOUT_SEC) still applies
# inside; this is the outer SSE budget. Overridable in tests via monkeypatch.
STREAM_TIMEOUT_SEC = 60.0

# Internal error codes surfaced to clients (no tracebacks, ever).
ERR_PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
ERR_PROVIDER_ERROR = "PROVIDER_ERROR"
ERR_RETRIEVAL_FAILURE = "RETRIEVAL_FAILURE"


def sse_event(name: str, payload: dict[str, Any]) -> str:
    """Render one typed SSE frame."""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def split_tokens(answer: str, chunk_words: int = 3) -> list[str]:
    """Split a grounded answer into ordered delivery deltas.

    Word-group chunking (not per-character): preserves order, keeps frame
    count bounded, and is deterministic for tests. True provider token
    streaming is used when the upstream gateway supports SSE
    (``LocalOpenAICompatLLM.chat_stream``); this splitter is the honest
    fallback that renders the grounded answer incrementally instead of
    buffering the whole response.
    """
    words = (answer or "").split()
    if not words:
        return []
    return [" ".join(words[i:i + chunk_words]) + " "
            for i in range(0, len(words), chunk_words)]


def citations_to_sources(citations: list[dict] | None) -> list[dict]:
    """Project agent citation cards to the stream ``sources`` shape."""
    sources = []
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        sources.append({
            "chunk_id": c.get("chunk_id", ""),
            "document": c.get("filename", ""),
            "tag": c.get("tag", ""),
        })
    return sources


# ---- Deferred session persistence ---------------------------------------
# All assistant/user writes go through SessionStore.append. During a stream we
# buffer appends for the streaming session key only; other sessions pass
# through untouched (concurrent-stream safe). On clean completion the caller
# flushes; on disconnect/timeout the caller discards — so partial content is
# never committed as a completed answer.

_buffer_lock = threading.Lock()
_buffered_keys: set[tuple] = set()
_buffered_writes: dict[tuple, list[tuple]] = {}
_patch_depth = 0
_true_append = None


def _patched_append(sid: str, role: str, content: str,
                    intent: str | None = None, tenant_id: str | None = None):
    """Single process-wide patch: buffer only keys under defer, else direct."""
    from .config import settings as _s

    k = ((tenant_id or _s.TENANT_ID), sid)
    with _buffer_lock:
        if k in _buffered_keys:
            _buffered_writes.setdefault(k, []).append(
                (sid, role, content, intent, tenant_id))
            return
        target = _true_append
    target(sid, role, content, intent, tenant_id=tenant_id)


def _session_key(tenant_id: str | None, session_id: str) -> tuple:
    from .config import settings

    return (tenant_id or settings.TENANT_ID, session_id)


@contextmanager
def defer_session_persist(tenant_id: str | None, session_id: str):
    """Buffer ``session_store.append`` for one session key.

    Yields a ``flush()`` callable. If the caller never calls it (disconnect,
    timeout, error), buffered writes are dropped on context exit.
    Re-entrant for the same key (refcounted); concurrent different sessions
    are independent.
    """
    from .agent.session import session_store

    global _patch_depth, _true_append
    key = _session_key(tenant_id, session_id)
    with _buffer_lock:
        if _patch_depth == 0:
            _true_append = session_store.append
            session_store.append = _patched_append  # type: ignore[method-assign]
        _patch_depth += 1
        _buffered_keys.add(key)
        _buffered_writes.setdefault(key, [])

    def flush() -> int:
        with _buffer_lock:
            writes = _buffered_writes.get(key, [])
            _buffered_writes[key] = []
            target = _true_append
        n = 0
        for (sid, role, content, intent, tid) in writes:
            target(sid, role, content, intent, tenant_id=tid)
            n += 1
        return n

    try:
        yield flush
    finally:
        with _buffer_lock:
            _buffered_writes.pop(key, None)
            _buffered_keys.discard(key)
            _patch_depth -= 1
            if _patch_depth <= 0:
                _patch_depth = 0
                if _true_append is not None:
                    session_store.append = _true_append  # type: ignore[method-assign]
                    _true_append = None


def buffered_count(tenant_id: str | None, session_id: str) -> int:
    """Test hook: how many writes are currently buffered for a session."""
    with _buffer_lock:
        return len(_buffered_writes.get(_session_key(tenant_id, session_id), []))
