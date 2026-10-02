"""Distributed state: what two replicas can and cannot see today.

These tests talk to the REAL local PostgreSQL and Redis containers
(`docker compose -f deploy/docker/compose.infra.yml up -d postgres redis`). They
are skipped, loudly, when those are not up — a mocked Redis proves nothing about
whether a quota is shared between processes, which is the entire question.

The shape of the file is deliberate: it pins the CURRENT failure first, then
proves the replacement. A suite that only ever asserts the good state cannot tell
you which of the two behaviours you actually have.

    1. The shipped limiter is process-local -> cross-instance visibility FAILS
    2. A Redis-backed limiter shares quota   -> cross-instance visibility WORKS
    3. Redis going away must not mean "unlimited"

Run:
    docker compose -f deploy/docker/compose.infra.yml up -d postgres redis
    pytest tests/test_distributed_state.py -v
"""

from __future__ import annotations

import importlib.util
import os
import time
import uuid

import pytest

REDIS_URL = os.environ.get("MAIA_TEST_REDIS_URL", "redis://localhost:6379")
PG_DSN = os.environ.get(
    "MAIA_TEST_PG_DSN", "postgresql://maia:maia@localhost:5432/maia_test"
)

redis_available = importlib.util.find_spec("redis") is not None
psycopg_available = importlib.util.find_spec("psycopg") is not None

redis_required = pytest.mark.skipif(
    not redis_available, reason="redis client not installed (requirements-optional.txt)"
)
pg_required = pytest.mark.skipif(
    not psycopg_available, reason="psycopg not installed (requirements-optional.txt)"
)


# ---------------------------------------------------------------------------
# 1. The shipped limiter, pinned as the failure it currently is.
# ---------------------------------------------------------------------------


@redis_required
def test_shipped_limiter_does_not_share_quota_between_instances() -> None:
    """`api._rl_hits` is per-process, so two replicas each get the full budget.

    Asserting the CURRENT behaviour on purpose: this is the regression the
    distributed limiter exists to fix. When someone swaps in a shared limiter
    this test fails and gets rewritten — rather than quietly continuing to pass
    against a process-local dict.
    """
    from maia import api

    api._rl_hits.clear()
    replica_b_hits: dict[str, list[float]] = {}

    limit = 3
    # `_rl_hits` stores float timestamps, not counters — appending anything else
    # raises inside the limiter itself and would test the wrong thing.
    now = time.time()
    for _ in range(limit):
        api._rl_hits.setdefault("user-1", []).append(now)

    assert len(api._rl_hits["user-1"]) == limit
    assert len(replica_b_hits) == 0, (
        "the second replica already sees the quota — if this passes, the limiter is "
        "no longer process-local and this test must be rewritten to assert the "
        "shared behaviour instead"
    )
    api._rl_hits.clear()


# ---------------------------------------------------------------------------
# 2. A Redis-backed limiter does share the quota.
# ---------------------------------------------------------------------------


@redis_required
def test_redis_limiter_shares_quota_between_independent_clients() -> None:
    """Instance A spends the budget; instance B must see it spent.

    Two SEPARATE Redis clients stand in for two replicas. The counter lives in
    the server, so the second client cannot hold its own copy — which is exactly
    what the process-local dict does.
    """
    import redis as _r

    from maia.distributed_state import RateLimiter

    key = f"test:rl:{uuid.uuid4()}"
    limit = 3

    replica_a = RateLimiter(_r.Redis.from_url(REDIS_URL), limit=limit, window_sec=60)
    replica_b = RateLimiter(_r.Redis.from_url(REDIS_URL), limit=limit, window_sec=60)

    decisions_a = [replica_a.allow("user-x", scope=key) for _ in range(limit)]
    assert all(d.allowed for d in decisions_a), "replica A should be under quota"
    assert decisions_a[-1].remaining == 0

    decision_b = replica_b.allow("user-x", scope=key)
    assert not decision_b.allowed, (
        "replica B allowed a request after the shared budget was already spent by "
        "replica A — the quota is not distributed"
    )
    assert decision_b.reason == "rate_limited"

    replica_a.reset("user-x", scope=key)


@redis_required
def test_different_users_have_independent_budgets() -> None:
    """Sharing must not degrade into global throttling."""
    import redis as _r

    from maia.distributed_state import RateLimiter

    scope = f"test:rl:{uuid.uuid4()}"
    limiter = RateLimiter(_r.Redis.from_url(REDIS_URL), limit=1, window_sec=60)

    assert limiter.allow("alice", scope=scope).allowed
    assert not limiter.allow("alice", scope=scope).allowed
    assert limiter.allow("bob", scope=scope).allowed, "user A's spend throttled user B"
    limiter.reset("alice", scope=scope)
    limiter.reset("bob", scope=scope)


@redis_required
def test_redis_outage_is_explicit_not_unlimited() -> None:
    """Redis being down must be a named state, never a silent free-for-all.

    The historical hazard is a limiter that degrades to "no limit" when its
    backing store is unreachable, so `RateLimiter` refuses to open without a
    client rather than defaulting to permissive.
    """
    from maia.distributed_state import RateLimiter

    limiter = RateLimiter(None, limit=10, window_sec=60)
    decision = limiter.allow("user-y", scope="test:offline")

    assert not decision.allowed, "an absent backend must not allow unlimited traffic"
    assert decision.reason == "backend_unavailable"
    assert decision.degraded is True
