"""Bounded lifetime for the streaming (MemorySaver) graph threads.

Covers the two independent limits in ``StreamingSessionRegistry``:

* **LRU cap** — the map never exceeds ``max_sessions``; the least recently used
  thread is the one evicted.
* **TTL** — a thread idle past ``ttl_sec`` is reclaimed on the next touch, which
  is what actually frees memory between traffic bursts.

Plus the properties that make it safe to put in front of a request path: a
failing evictor never breaks the caller, ``release`` frees a thread
immediately, and non-positive limits are clamped rather than trusted.

A fake clock is injected rather than sleeping, so the TTL cases are
deterministic and the suite stays fast.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.session_registry import StreamingSessionRegistry


class FakeClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _registry(evicted, *, max_sessions=3, ttl_sec=100, clock=None):
    return StreamingSessionRegistry(
        max_sessions=max_sessions,
        ttl_sec=ttl_sec,
        on_evict=evicted.append,
        clock=clock,
    )


# --- LRU cap ------------------------------------------------------------- #

def test_keeps_threads_under_the_cap():
    reg = _registry([], max_sessions=3)
    for i in range(10):
        reg.track(f"t{i}")
    assert len(reg) == 3


def test_evicts_least_recently_used_first():
    evicted = []
    reg = _registry(evicted, max_sessions=2)
    reg.track("old")
    reg.track("mid")
    reg.track("new")
    # "old" is least recently used → the one that has to go.
    assert evicted == ["old"]


def test_retracking_a_thread_protects_it_from_eviction():
    evicted = []
    reg = _registry(evicted, max_sessions=2)
    reg.track("a")
    reg.track("b")
    reg.track("a")   # "a" is now the most recent, "b" becomes the oldest
    reg.track("c")
    assert evicted == ["b"]
    assert "a" in reg.live_threads()


def test_cap_of_one_keeps_only_the_newest_thread():
    evicted = []
    reg = _registry(evicted, max_sessions=1)
    reg.track("a")
    reg.track("b")
    assert evicted == ["a"]
    assert reg.live_threads() == ["b"]




# --- TTL ----------------------------------------------------------------- #

def test_expires_threads_idle_past_the_ttl():
    clock = FakeClock()
    evicted = []
    reg = _registry(evicted, max_sessions=100, ttl_sec=60, clock=clock)
    reg.track("stale")
    clock.advance(61)
    reg.track("fresh")
    assert evicted == ["stale"]
    assert reg.live_threads() == ["fresh"]


def test_thread_within_ttl_survives():
    clock = FakeClock()
    evicted = []
    reg = _registry(evicted, max_sessions=100, ttl_sec=60, clock=clock)
    reg.track("a")
    clock.advance(59)
    reg.track("b")
    assert evicted == []
    assert set(reg.live_threads()) == {"a", "b"}


def test_touch_refreshes_the_idle_clock():
    clock = FakeClock()
    evicted = []
    reg = _registry(evicted, max_sessions=100, ttl_sec=60, clock=clock)
    reg.track("a")
    for _ in range(5):
        clock.advance(50)
        reg.track("a")   # each touch resets the idle timer
    clock.advance(10)
    reg.track("b")
    assert evicted == []
    assert "a" in reg.live_threads()


def test_ttl_of_zero_disables_expiry():
    clock = FakeClock()
    evicted = []
    reg = _registry(evicted, max_sessions=100, ttl_sec=0, clock=clock)
    reg.track("a")
    clock.advance(10_000)
    reg.track("b")
    assert evicted == []
    assert set(reg.live_threads()) == {"a", "b"}


# --- release ------------------------------------------------------------- #

def test_release_frees_a_thread_immediately():
    evicted = []
    reg = _registry(evicted, max_sessions=10, ttl_sec=0)
    reg.track("gone")
    reg.release("gone")
    assert evicted == ["gone"]
    assert reg.live_threads() == []


def test_release_of_an_untracked_thread_is_a_noop():
    evicted = []
    reg = _registry(evicted)
    reg.release("never-seen")
    assert evicted == []


# --- robustness ---------------------------------------------------------- #

def test_evictor_failure_never_propagates():
    def boom(_thread_id):
        raise RuntimeError("checkpointer unavailable")

    reg = StreamingSessionRegistry(max_sessions=1, ttl_sec=0, on_evict=boom)
    reg.track("a")
    reg.track("b")   # evicts "a" → boom, but must not raise
    assert reg.live_threads() == ["b"]


def test_registry_works_without_an_evictor():
    reg = StreamingSessionRegistry(max_sessions=2, ttl_sec=0, on_evict=None)
    for i in range(5):
        reg.track(f"t{i}")
    assert len(reg) == 2


def test_empty_thread_id_is_ignored():
    reg = _registry([], max_sessions=5)
    reg.track("")
    assert len(reg) == 0


def test_max_sessions_zero_does_not_track_anything():
    evicted = []
    reg = _registry(evicted, max_sessions=0)
    reg.track("a")
    reg.track("b")
    # Budget disabled → the map is kept empty rather than growing unbounded.
    assert len(reg) == 0
    assert evicted == []


def test_max_sessions_zero_evicts_anything_tracked_before_the_budget_dropped():
    evicted = []
    reg = _registry(evicted, max_sessions=3, ttl_sec=0)
    reg.track("a")
    reg.track("b")
    reg._max = 0          # operator sets MAX_STREAMING_SESSIONS=0 at runtime
    reg.track("c")
    # Everything previously held is released rather than kept forever.
    assert evicted == ["a", "b"]
    assert len(reg) == 0


def test_negative_limits_are_clamped_to_zero():
    reg = StreamingSessionRegistry(max_sessions=-5, ttl_sec=-1)
    assert reg.max_sessions == 0
    assert reg.ttl_sec == 0
    reg.track("a")
    assert len(reg) == 0


def test_live_threads_lists_most_recent_last():
    reg = _registry([], max_sessions=5, ttl_sec=0)
    reg.track("a")
    reg.track("b")
    reg.track("c")
    assert reg.live_threads() == ["a", "b", "c"]


def test_release_frees_capacity_for_the_next_thread():
    evicted = []
    reg = _registry(evicted, max_sessions=2, ttl_sec=0)
    reg.track("a")
    reg.track("b")
    reg.release("a")
    reg.track("c")
    # "a" was released, so only "b" was ever eligible for LRU eviction.
    assert evicted == ["a"]
    assert reg.live_threads() == ["b", "c"]

def test_eviction_stops_at_the_cap_under_a_burst():
    evicted = []
    reg = _registry(evicted, max_sessions=5, ttl_sec=0)
    for i in range(100):
        reg.track(f"burst{i}")
        assert len(reg) <= 5
    assert len(evicted) == 95
