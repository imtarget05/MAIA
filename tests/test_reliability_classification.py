"""M4 — provider reliability: retry, breaker, and correct classification.

The interview question is "what if the LLM returns 503?" and the honest answer
has to distinguish WHICH failures are worth retrying. Retrying a 400 or a 401
is not resilience, it is amplifying a permanent error against someone else's
rate limit.

This module therefore asserts classification, not just that retry happens.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.loops.resilience import (  # noqa: E402
    CircuitBreaker,
    CircuitOpenError,
    RetryConfig,
    with_retry,
)


def _cfg(**kw) -> RetryConfig:
    """Fast config so the suite does not sleep through real backoff."""
    base = {"max_retries": 2, "backoff_base": 0.001}
    base.update(kw)
    return RetryConfig(**base)


# ---------------------------------------------------------------------------
# Retry: only transient failures are retried
# ---------------------------------------------------------------------------
def test_transient_timeout_is_retried_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("upstream timeout")
        return "ok"

    assert with_retry(_cfg(), flaky) == "ok"
    assert calls["n"] == 3, "should try three times before succeeding"


def test_retry_budget_exhausted_raises_the_last_error():
    """Explicit failure, never a silent fabricated success."""
    calls = {"n": 0}

    def always_timeout():
        calls["n"] += 1
        raise TimeoutError("still down")

    # Separated so the raising call is unambiguous to static analysis.
    with pytest.raises(TimeoutError):
        with_retry(_cfg(max_retries=2), always_timeout)

    assert calls["n"] == 3, "1 initial attempt + 2 retries, then stop"


def test_non_retryable_error_fails_immediately():
    """A bad request must not be retried — retrying cannot fix it."""
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        raise ValueError("malformed prompt: unclosed tag")

    with pytest.raises(ValueError):
        with_retry(_cfg(), bad_request)

    assert calls["n"] == 1, "a non-transient error must not be retried"


def test_auth_failure_is_not_retried():
    """401 will still be 401 on attempt two."""
    import requests

    calls = {"n": 0}

    def unauthorized():
        calls["n"] += 1
        resp = requests.Response()
        resp.status_code = 401
        raise requests.exceptions.HTTPError("401 Client Error", response=resp)

    with pytest.raises(requests.exceptions.HTTPError):
        with_retry(_cfg(), unauthorized)

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# HTTP status classification — the part that was previously implicit
# ---------------------------------------------------------------------------
def test_http_status_classification_matches_retry_policy():
    """429/5xx retry; 4xx (except 429) do not.

    This is the distinction `retryable=(TimeoutError, ConnectionError, OSError)`
    cannot express on its own. `requests.HTTPError` subclasses `OSError`, so
    EVERY http error — including 400 and 401 — satisfies that tuple. The tuple
    is therefore necessary but not sufficient, and this test pins the rule that
    the tuple alone leaves implicit.
    """
    from maia.agent.reliability import classify_http_status

    for status in (429, 500, 502, 503, 504):
        assert classify_http_status(status) == "retryable", status
    for status in (400, 401, 403, 404, 422):
        assert classify_http_status(status) == "fatal", status


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def test_breaker_opens_after_threshold_and_short_circuits():
    breaker = CircuitBreaker("llm", failure_threshold=3, recovery_timeout=60)

    def boom():
        raise TimeoutError("upstream down")

    for _ in range(3):
        with pytest.raises(TimeoutError):
            breaker.call(boom)

    # Now open: the wrapped function must not be invoked at all.
    invoked = {"n": 0}

    def should_not_run():
        invoked["n"] += 1
        return "unreachable"

    with pytest.raises(CircuitOpenError):
        breaker.call(should_not_run)
    assert invoked["n"] == 0, "an open breaker must not call the dependency"


def test_breaker_half_opens_after_recovery_timeout():
    breaker = CircuitBreaker("llm", failure_threshold=1, recovery_timeout=0.05)

    def boom():
        raise TimeoutError("down")

    with pytest.raises(TimeoutError):
        breaker.call(boom)
    with pytest.raises(CircuitOpenError):
        breaker.call(boom)

    time.sleep(0.08)  # past recovery_timeout

    # Half-open: the dependency is tried again, and success closes the circuit.
    assert breaker.call(lambda: "recovered") == "recovered"
    assert breaker.call(lambda: "still fine") == "still fine"


def test_breaker_success_resets_the_failure_count():
    breaker = CircuitBreaker("llm", failure_threshold=3, recovery_timeout=60)

    def boom():
        raise TimeoutError("down")

    for _ in range(2):
        with pytest.raises(TimeoutError):
            breaker.call(boom)
    breaker.call(lambda: "ok")
    # The two earlier failures must not count toward opening the circuit.
    with pytest.raises(TimeoutError):
        breaker.call(boom)
    assert breaker.call(lambda: "ok") == "ok", "breaker should still be closed"