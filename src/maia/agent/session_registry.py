"""Bounded lifetime for the streaming (MemorySaver) graph threads.

The SSE endpoint runs the agent on a **process-wide in-memory checkpointer**
keyed by ``"{tenant_id}:{session_id}"``. Nothing else ever releases those
threads, so a client that keeps opening new sessions grows the checkpointer
without bound for the life of the process — a slow leak that only shows up as
memory pressure long after it started.

:class:`StreamingSessionRegistry` bounds that map with two independent limits:

* **LRU cap** — once ``max_sessions`` threads are live, the least recently used
  one is evicted. Bounded by construction, no timer needed.
* **TTL** — a thread idle for longer than ``ttl_sec`` is evicted on the next
  touch. This is what actually reclaims memory between traffic bursts; the LRU
  cap alone only holds the ceiling steady.

Both are recorded in settings (``MAX_STREAMING_SESSIONS`` /
``STREAMING_SESSION_TTL_SEC``) so operators can tune them without a code change.

Scope is deliberately narrow: this guards the **streaming** graph only. The
durable (SqliteSaver) graph backs human-in-the-loop approval, where a paused
run must survive until somebody answers — arbitrarily long — so it is never
evicted from here. See ``langgraph_agent.get_durable_graph``.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from ..config import settings

__all__ = ["StreamingSessionRegistry", "streaming_sessions"]


class StreamingSessionRegistry:
    """LRU + TTL registry of live streaming threads.

    ``on_evict`` is called with each thread_id that falls out of the budget, so
    the caller can drop the corresponding LangGraph checkpoint. It is invoked
    while no lock is held, and its exceptions are swallowed: a failure to free
    one thread must never fail the request that triggered the sweep.
    """

    def __init__(
        self,
        *,
        max_sessions: int | None = None,
        ttl_sec: int | None = None,
        on_evict: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max = max(0, max_sessions if max_sessions is not None
                        else settings.MAX_STREAMING_SESSIONS)
        self._ttl = max(0, ttl_sec if ttl_sec is not None
                        else settings.STREAMING_SESSION_TTL_SEC)
        self._on_evict = on_evict
        self._clock = clock or time.monotonic
        # thread_id -> last-seen timestamp. OrderedDict so the first key is
        # always the least recently used; move_to_end on every touch keeps that
        # invariant true.
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def max_sessions(self) -> int:
        return self._max

    @property
    def ttl_sec(self) -> int:
        return self._ttl

    def track(self, thread_id: str) -> None:
        """Record use of ``thread_id`` and evict whatever the budget no longer allows."""
        if not thread_id:
            return
        evicted: list[str] = []
        with self._lock:
            if self._max == 0:
                # Budget disabled: drop everything rather than track unbounded.
                evicted = list(self._entries)
                self._entries.clear()
            else:
                now = self._clock()
                if self._ttl:
                    for expired in [t for t, seen in self._entries.items()
                                    if now - seen > self._ttl]:
                        del self._entries[expired]
                        evicted.append(expired)
                # Re-insert/move so this thread is the most recently used.
                if thread_id in self._entries:
                    self._entries.move_to_end(thread_id)
                self._entries[thread_id] = now
                while len(self._entries) > self._max:
                    evicted.append(self._entries.popitem(last=False)[0])
        self._fire(evicted)

    def release(self, thread_id: str) -> None:
        """Forget ``thread_id`` and free its checkpoint (e.g. client disconnect)."""
        with self._lock:
            existed = self._entries.pop(thread_id, None) is not None
        if existed:
            self._fire([thread_id])

    def _fire(self, thread_ids: list[str]) -> None:
        if not self._on_evict:
            return
        for thread_id in thread_ids:
            try:
                self._on_evict(thread_id)
            except Exception:
                # Freeing one thread is best-effort; never break the request.
                pass

    def live_threads(self) -> list[str]:
        """Snapshot of tracked thread_ids, most recently used last."""
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _free_streaming_thread(thread_id: str) -> None:
    """Drop a thread from the in-memory checkpointer behind the module graph.

    Imported lazily: ``langgraph_agent`` imports this module's sibling
    ``config`` and the API imports both, so a top-level import would be fine
    but keeps the failure blast radius smaller this way.
    """
    try:
        from .langgraph_agent import graph

        checkpointer = graph.checkpointer
        if checkpointer is None or isinstance(checkpointer, bool):
            return
        checkpointer.delete_thread(thread_id)
    except Exception:
        pass


streaming_sessions = StreamingSessionRegistry(on_evict=_free_streaming_thread)
