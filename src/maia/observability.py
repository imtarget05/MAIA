"""MAIA-07: 7-stage pipeline tracing with correlation_id.

Generates a correlation_id at request entry, propagates it through all 7 stages
(query_input → retrieval → rerank → evidence_gate → guardrail → generation →
response). Each stage emits exactly one structured log entry.

The structured log is always on (not gated) — the whole point of MAIA-07 is
debugging leakage/false-refusal cases that MAIA-01 surfaced.

An additional `_trace` object embedded in the query response is gated behind
settings.PIPELINE_TRACE (default False) and never echoes raw PII/chunk content
back to the client.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("maia.pipeline")


@dataclass
class Span:
    correlation_id: str
    stage: str
    ts: float
    fields: dict = field(default_factory=dict)


class PipelineTracer:
    """Trace a single request across the 7 pipeline stages."""

    STAGES = ("query_input", "retrieval", "rerank", "evidence_gate",
              "guardrail", "generation", "response")

    def __init__(self, correlation_id: str | None = None):
        self.correlation_id = correlation_id or uuid.uuid4().hex[:12]
        self.spans: list[Span] = []

    def log(self, stage: str, **fields: Any) -> None:
        """Emit one structured log entry for a pipeline stage."""
        if stage not in self.STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {self.STAGES}")
        span = Span(correlation_id=self.correlation_id, stage=stage,
                     ts=time.time(), fields=fields)
        self.spans.append(span)
        logger.info(
            "maia_stage",
            extra={
                "correlation_id": self.correlation_id,
                "stage": stage,
                "fields": fields,
            },
        )

    def finalize(self) -> dict:
        """Return the full trace as a dict (for _trace embedding)."""
        return {
            "correlation_id": self.correlation_id,
            "n_stages": len(self.spans),
            "stages": [
                {"stage": s.stage, "ts": round(s.ts, 6), "fields": s.fields}
                for s in self.spans
            ],
        }

    def finalize_redacted(self) -> dict:
        """Return a PII-safe trace for client echoing.

        Drops raw text/PII-bearing fields per G-04 — keeps only structural
        signal (scores, counts, flags, refusal reasons) so the client never
        sees chunk content or PII.
        """
        safe_fields = {}
        for s in self.spans:
            redacted = {}
            for k, v in s.fields.items():
                if k in ("query", "text", "chunk_text", "context", "answer"):
                    continue  # drop raw text / PII-bearing fields
                redacted[k] = v
            safe_fields[s.stage] = redacted
        return {
            "correlation_id": self.correlation_id,
            "n_stages": len(self.spans),
            "stages": safe_fields,
        }
