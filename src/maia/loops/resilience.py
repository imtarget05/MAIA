"""G-06: reliability controls for MAIA's external dependencies.

Two mechanisms, composable:

  * with_retry(config, fn, *args)  — exponential-backoff retry on transient
    errors only (Timeout / ConnectionError / OSError). 4xx-style client errors
    fail immediately, no retry.

  * CircuitBreaker              — per-dependency, in-memory (per-process).
    CLOSED  → requests flow, failures counted.
    OPEN    → after N consecutive failures, calls short-circuit with
              CircuitOpenError (no request sent). After a cool-down window the
    HALF_OPEN breaker admits one probe request; success → CLOSED, failure → OPEN.

    Known limitation (noted for future work): state is per-process. Fine at
    current single-replica scale; revisit with a shared store (Redis / Qdrant
    flag) if MAIA scales to multiple workers/replicas.

Usage::

    breaker = CircuitBreaker("qdrant", failure_threshold=5, recovery_timeout=30.0)
    return breaker.call(client.search, query_vec, limit=10)

    # or with retry wrapped around the breaker:
    return with_retry(retry_cfg, breaker.call, client.search, query_vec)
"""
from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# --- retry ------------------------------------------------------------------

@dataclass
class RetryConfig:
    """Retry policy. Only `retryable` exception types trigger a retry."""
    max_retries: int = 2
    backoff_base: float = 0.25          # seconds; doubles each attempt
    retryable: tuple[type[Exception], ...] = (TimeoutError, ConnectionError, OSError)


class CircuitOpenError(Exception):
    """Raised by CircuitBreaker.call when the breaker is OPEN (short-circuit)."""


def with_retry(config: RetryConfig, fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Call `fn(*args, **kwargs)` with exponential-backoff retry.

    Retries only on exceptions in `config.retryable`. Any other exception
    propagates immediately. Returns fn's result, or raises the last exception
    after `config.max_retries` failed attempts.
    """
    last_exc: Exception | None = None
    for attempt in range(config.max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except config.retryable as e:
            last_exc = e
            if attempt < config.max_retries:
                time.sleep(config.backoff_base * (2 ** attempt))
            # else: fall through and raise
        except Exception:
            raise
    raise last_exc  # type: ignore[misc]


# --- circuit breaker --------------------------------------------------------

@dataclass
class CircuitBreaker:
    """Per-dependency circuit breaker with CLOSED / OPEN / HALF_OPEN states.

    Thread-unsafe by design (single-threaded async / per-process use). See
    module docstring for the multi-replica limitation.
    """

    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0          # seconds in OPEN before HALF_OPEN
    _state: str = field(default="closed", repr=False)
    _failures: int = field(default=0, repr=False)
    _last_failure_ts: float = field(default=0.0, repr=False)

    # state constants
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    @property
    def state(self) -> str:
        # auto-transition OPEN → HALF_OPEN when cool-down has elapsed
        if self._state == self.OPEN and time.time() - self._last_failure_ts >= self.recovery_timeout:
            self._state = self.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_ts = time.time()
        if self._failures >= self.failure_threshold:
            self._state = self.OPEN

    def _allow_request(self) -> bool:
        """Return True if a request should be attempted in the current state."""
        _ = self.state  # trigger auto-transition
        return self._state != self.OPEN

    def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Call `fn(*args, **kwargs)` if the breaker allows it.

        Raises CircuitOpenError when OPEN (short-circuit, fn never invoked).
        On success → record_success; on any exception → record_failure and
        re-raise the original exception.
        """
        if not self._allow_request():
            raise CircuitOpenError(
                f"Circuit breaker '{self.name}' is OPEN "
                f"({self._failures}/{self.failure_threshold} failures, "
                f"cool-down {self.recovery_timeout}s)"
            )
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()
            return result


@functools.cache
def get_breaker(name: str, failure_threshold: int = 5,
                recovery_timeout: float = 30.0) -> CircuitBreaker:
    """Process-wide singleton breaker per dependency name."""
    return CircuitBreaker(name, failure_threshold, recovery_timeout)
