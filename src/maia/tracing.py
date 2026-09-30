"""MAIA OpenTelemetry tracing (opt-in OTLP exporter).

Mirrors the 7-stage ``PipelineTracer`` (see ``maia.observability``) as OTEL
spans so ``query()`` stages can be shipped to any OTLP-compatible backend
(Jaeger / Tempo / Collector). Reference port: FlashSale-Backend
``Order.Api/Program.cs`` (OTLP + ``OTEL_EXPORTER_OTLP_ENDPOINT``).

Opt-in by design:

- ``OTEL_ENABLED=false`` (default) → every helper here is a NoOp and the
  ``opentelemetry-*`` packages are not even imported, so MAIA runs unchanged
  when they are not installed.
- ``OTEL_ENABLED=true`` + ``OTEL_EXPORTER_OTLP_ENDPOINT`` set →
  ``init_tracing()`` installs an SDK ``TracerProvider`` with a batching OTLP
  exporter under the ``OTEL_SERVICE_NAME`` resource (default ``"maia"``).

Every ``opentelemetry`` import is lazy and guarded by ``try/except`` so a
missing/broken install degrades to NoOp instead of breaking the request path.
Span attributes go through :func:`sanitize_attributes`, which reuses
``maia.observability.REDACTED_FIELDS`` — structural signal only
(counts, scores, flags), never raw query/chunk text or PII.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Self

logger = logging.getLogger("maia.tracing")

# Reused redaction list — single source of truth lives in observability so the
# log-based trace and the OTEL spans can never disagree on what is PII-bearing.
try:
    from .observability import REDACTED_FIELDS
except Exception:  # pragma: no cover - importable in practice, fallback for safety
    REDACTED_FIELDS = frozenset({"query", "text", "chunk_text", "context", "answer"})

_TRACER_NAME = "maia.pipeline"

_initialized = False


def is_enabled() -> bool:
    """True only when the operator opted in AND an endpoint is configured."""
    try:
        from .config import settings
    except Exception:
        return False
    return bool(settings.OTEL_ENABLED) and bool(settings.OTEL_EXPORTER_OTLP_ENDPOINT)


def sanitize_attributes(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Drop PII/text-bearing keys; keep structural signal (counts/scores/flags)."""
    if not attrs:
        return {}
    return {k: v for k, v in attrs.items() if k not in REDACTED_FIELDS}


# --------------------------------------------------------------------------- #
# NoOp fallbacks (used when OTEL is disabled or its packages are absent)
# --------------------------------------------------------------------------- #
class NoOpSpan:
    """Minimal span-compatible shim: usable as a context manager, swallows sets."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def set_attribute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        pass

    def end(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_span_context(self) -> Any:
        try:
            from opentelemetry.trace import INVALID_SPAN_CONTEXT  # type: ignore

            return INVALID_SPAN_CONTEXT
        except Exception:
            return None


class NoOpTracer:
    """Tracer-compatible shim returning :class:`NoOpSpan` instances."""

    def start_span(self, *args: Any, **kwargs: Any) -> NoOpSpan:
        return NoOpSpan()

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> NoOpSpan:
        return NoOpSpan()


_NOOP_TRACER = NoOpTracer()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_tracer(name: str = _TRACER_NAME) -> Any:
    """Return a real OTEL tracer when enabled, else a NoOp tracer.

    Never raises: any import/setup failure falls back to NoOp.
    """
    if not is_enabled():
        return _NOOP_TRACER
    try:
        from opentelemetry import trace  # type: ignore

        return trace.get_tracer(name)
    except Exception as exc:
        logger.debug("otel tracer unavailable, using NoOp: %s", exc)
        return _NOOP_TRACER


def init_tracing() -> bool:
    """Install the SDK TracerProvider + OTLP exporter. Idempotent.

    Returns True when a working OTLP pipeline was installed, False when
    tracing stays NoOp (disabled / no endpoint / packages missing / error).
    """
    global _initialized
    if _initialized:
        return True
    if not is_enabled():
        return False
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore

        from .config import settings

        # Respect an already-configured provider (e.g. tests installing an
        # InMemorySpanExporter, or a host app that configured OTEL itself).
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            _initialized = True
            return True

        resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME or "maia"})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT))
        )
        trace.set_tracer_provider(provider)
        _initialized = True
        logger.info("otel tracing enabled: service=%s endpoint=%s",
                    settings.OTEL_SERVICE_NAME, settings.OTEL_EXPORTER_OTLP_ENDPOINT)
        return True
    except Exception as exc:
        logger.warning("otel init failed, tracing stays NoOp: %s", exc)
        return False


@contextmanager
def span_context(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """Open a child span of the current span (or a NoOp span when disabled).

    Attributes are sanitized (PII/text keys dropped) before being attached.
    Yields the span so callers can ``set_attribute`` after the wrapped call::

        with span_context("maia.pipeline.retrieval", {"tenant": t}) as span:
            cands = retriever.retrieve(q)
            span.set_attribute("n_candidates", len(cands))
    """
    safe = sanitize_attributes(attributes)
    tracer = get_tracer()
    start = getattr(tracer, "start_as_current_span", None)
    if not callable(start):
        yield NoOpSpan()
        return
    try:
        cm: Any = start(name, attributes=safe or None)
    except Exception as exc:
        logger.debug("otel span %r failed to start, continuing without it: %s", name, exc)
        yield NoOpSpan()
        return
    # Body exceptions must propagate (never swallow into a second yield —
    # that breaks the generator protocol with "didn't stop after throw()").
    with cm as span:
        yield span


def start_span(name: str, attributes: dict[str, Any] | None = None) -> Any:
    """Start a span without attaching it to the current context.

    Returns a real span (caller must ``.end()`` it) or a :class:`NoOpSpan`
    when OTEL is disabled/absent. Used for the request root span in
    ``pipeline_query.query()``; stage spans should prefer :func:`span_context`
    so they nest automatically.
    """
    safe = sanitize_attributes(attributes)
    tracer = get_tracer()
    start = getattr(tracer, "start_span", None)
    if not callable(start):
        return NoOpSpan()
    try:
        return start(name, attributes=safe or None)
    except Exception as exc:
        logger.debug("otel start_span %r failed: %s", name, exc)
        return NoOpSpan()


__all__ = [
    "REDACTED_FIELDS",
    "NoOpSpan",
    "NoOpTracer",
    "get_tracer",
    "init_tracing",
    "is_enabled",
    "sanitize_attributes",
    "span_context",
    "start_span",
]
