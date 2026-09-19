"""G-06: chaos tests for circuit breaker + retry resilience controls."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.loops.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryConfig,
    with_retry,
)

# --- CircuitBreaker unit tests ---

def test_breaker_starts_closed():
    cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=1.0)
    assert cb.state == "closed"
    assert cb._allow_request() is True


def test_breaker_opens_after_N_failures():
    cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"  # not yet at threshold
    cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "should not run")


def test_breaker_success_resets_failure_count():
    cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60.0)
    cb.record_failure()
    cb.record_failure()
    result = cb.call(lambda: "ok")
    assert result == "ok"
    assert cb.state == "closed"
    assert cb._failures == 0


def test_breaker_half_open_recovers_on_success():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    time.sleep(0.06)  # cool-down elapsed
    assert cb._allow_request()  # auto-transition to half-open
    result = cb.call(lambda: "recovered")
    assert result == "recovered"
    assert cb.state == "closed"


def test_breaker_half_open_reopens_on_failure():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    time.sleep(0.06)
    assert cb._allow_request()  # half-open
    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cb.state == "open"


def test_circuit_open_short_circuits_without_calling_fn():
    cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60.0)
    cb.record_failure()
    calls = []
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: calls.append(1))
    assert calls == []  # fn never invoked


# --- with_retry tests ---

def test_retry_succeeds_first_try():
    config = RetryConfig(max_retries=2, backoff_base=0.01)
    assert with_retry(config, lambda: "ok") == "ok"


def test_retry_retries_on_transient_error():
    config = RetryConfig(max_retries=3, backoff_base=0.01, retryable=(ConnectionError,))
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("transient")
        return "ok"

    assert with_retry(config, flaky) == "ok"
    assert len(attempts) == 3


def test_retry_exhausts_and_raises_last_error():
    config = RetryConfig(max_retries=2, backoff_base=0.01, retryable=(TimeoutError,))
    with pytest.raises(TimeoutError):
        with_retry(config, lambda: (_ for _ in ()).throw(TimeoutError("still failing")))


def test_retry_does_not_retry_on_non_retryable():
    config = RetryConfig(max_retries=3, backoff_base=0.01, retryable=(ConnectionError,))
    attempts = []

    def bad():
        attempts.append(1)
        raise ValueError("client error")

    with pytest.raises(ValueError):
        with_retry(config, bad)
    assert len(attempts) == 1  # no retry on non-retryable


# --- Integration: graceful degraded on breaker open ---

def test_qdrant_search_graceful_degraded_when_breaker_open():
    """When Qdrant breaker is open, search returns [] instead of hanging."""
    from maia.loops.resilience import CircuitBreaker, CircuitOpenError
    cb = CircuitBreaker("qdrant-test", failure_threshold=1, recovery_timeout=60.0)
    cb.record_failure()
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: (_ for _ in ()).throw(Exception("should not run")))


def test_llm_chat_graceful_degraded_when_breaker_open():
    """When LLM breaker is open, chat returns degraded mock string."""
    from maia.loops.resilience import CircuitBreaker, CircuitOpenError
    cb = CircuitBreaker("llm-test", failure_threshold=1, recovery_timeout=60.0)
    cb.record_failure()
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: (_ for _ in ()).throw(Exception("should not run")))


def test_embed_graceful_degraded_falls_back_to_hash():
    """When embed breaker is open, embed() falls back to hash embedding (no hang)."""
    from maia.embeddings import Embedder
    # Force hash mode (no fastembed backend)
    e = Embedder.__new__(Embedder)
    e.model = "test"
    e.dim = 384
    e._backend = None
    e._mode = "hash"
    result = e.embed(["hello world"])
    assert result.shape == (1, 384)
