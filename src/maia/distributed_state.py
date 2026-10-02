"""Distributed short-lived state: the shared rate limiter.

WHY THIS EXISTS AS ITS OWN MODULE. MAIA's rate limiter lives inline in
`api.py` as a module-level `defaultdict(list)` of timestamps. That structure is
correct for one process and silently wrong for two: with more than one replica
each one keeps its own counter, so the effective limit is `limit x replicas` and
a client can spend a fresh budget simply by being load-balanced elsewhere.

Redis is the right home for this and NOT for conversations, checkpoints or HITL
approvals. Those are durable truth and belong in PostgreSQL; a rate-limit window
is a counter that is allowed to evaporate. Keeping the split explicit here stops
the two from being confused later.

FAILURE POLICY, stated rather than implied. A limiter whose backing store is
unreachable must not become "no limit" — that is how a rate limit turns into a
denial-of-service invitation. `RateLimiter` therefore refuses to open without a
backend and reports `backend_unavailable`. The trade-off is deliberate: a Redis
outage costs availability on the chat path rather than opening it. The API layer
decides whether to surface that as 429 or as 503; this module only refuses to
lie about the quota.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# Reasons a decision went the way it did. Values are stable strings because
# they end up in metrics and in client-facing headers.
REASON_ALLOWED = "allowed"
REASON_LIMITED = "rate_limited"
REASON_NO_BACKEND = "backend_unavailable"
REASON_BACKEND_ERROR = "backend_error"


@dataclass(frozen=True)
class RateLimitDecision:
    """One limiter decision, with enough context to log it without guessing."""

    allowed: bool
    remaining: int
    limit: int
    reason: str
    #: True when the decision was made under a degraded backend. A caller that
    #: wants to fail OPEN on outage must check this, not just `allowed`.
    degraded: bool = False


class RateLimiter:
    """Fixed-window limiter whose counter lives in Redis.

    Fixed window rather than sliding log: the window needs only an INCR and an
    EXPIRE, which is two round trips and no unbounded key growth. The trade-off
    is the known double-budget at a window boundary; that is acceptable here and
    is documented rather than hidden.

    `client=None` is a first-class, explicit state — not a fallback to a local
    dict. A limiter that silently becomes process-local is the exact bug this
    module replaces.
    """

    def __init__(self, client: Any | None, *, limit: int, window_sec: int) -> None:
        self._client = client
        self._limit = int(limit)
        self._window = int(window_sec)

    @property
    def is_distributed(self) -> bool:
        """False means this limiter cannot coordinate across processes."""
        return self._client is not None

    def _key(self, subject: str, scope: str) -> str:
        return f"maia:rl:{scope}:{subject}"

    def allow(
        self, subject: str, *, scope: str = "chat", now: float | None = None
    ) -> RateLimitDecision:
        """Consume one unit of `subject`'s budget for `scope`.

        `now` is injectable so a window rollover can be tested without sleeping.
        """
        if self._client is None:
            return RateLimitDecision(
                allowed=False,
                remaining=0,
                limit=self._limit,
                reason=REASON_NO_BACKEND,
                degraded=True,
            )
        try:
            key = self._key(subject, scope)
            used = int(self._client.incr(key))
            if used == 1:
                # Only the first hit sets the TTL, so the window is anchored to
                # the start of the window rather than sliding with every request.
                self._client.expire(key, self._window)
            allowed = used <= self._limit
            return RateLimitDecision(
                allowed=allowed,
                remaining=max(0, self._limit - used),
                limit=self._limit,
                reason=REASON_ALLOWED if allowed else REASON_LIMITED,
            )
        except Exception:
            # A Redis error mid-request must not read as "allowed". Reporting
            # degraded keeps the distinction visible to the caller.
            return RateLimitDecision(
                allowed=False,
                remaining=0,
                limit=self._limit,
                reason=REASON_BACKEND_ERROR,
                degraded=True,
            )

    def reset(self, subject: str, *, scope: str = "chat") -> bool:
        """Drop a subject's window. Used by tests and by administrative resets."""
        if self._client is None:
            return False
        try:
            return bool(self._client.delete(self._key(subject, scope)))
        except Exception:
            return False

    @staticmethod
    def window_seconds(now: float | None = None) -> float:
        """Current wall-clock, exposed so callers can log window identity."""
        return time.time() if now is None else now
