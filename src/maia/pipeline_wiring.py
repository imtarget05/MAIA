"""Cross-cutting pipeline wiring: shared singletons for document sanitization,
PII scanning, and pipeline tracing.

Separated from ingestion_pipeline.py and pipeline_query.py to isolate
cross-cutting wiring from ingestion and retrieval concerns.
"""
from .loops.guardrails import DocumentSanitizer
from .loops.pii import PIIScanner
from .observability import PipelineTracer

_doc_sanitizer = DocumentSanitizer()
_pii_scanner = PIIScanner()


def get_doc_sanitizer() -> DocumentSanitizer:
    return _doc_sanitizer


def get_pii_scanner() -> PIIScanner:
    return _pii_scanner


def get_pipeline_tracer() -> PipelineTracer:
    return PipelineTracer()


__all__ = [
    "PipelineTracer",
    "_doc_sanitizer",
    "_pii_scanner",
    "get_doc_sanitizer",
    "get_pii_scanner",
    "get_pipeline_tracer",
]
