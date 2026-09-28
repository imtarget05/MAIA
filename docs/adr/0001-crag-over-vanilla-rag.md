# ADR-0001: CRAG (Corrective RAG) over Vanilla RAG

- **Status:** Accepted
- **Date:** 2026-09-27

## Context

Vanilla retrieve-then-generate answers even when retrieval returns junk: the
LLM fluently grounds on irrelevant chunks, which for HR/Security policy is a
liability, not a feature. The system needs a principled "retrieve → grade →
correct-or-refuse" loop instead of blind generation.

## Decision

Adopt Corrective RAG: after hybrid retrieval (Qdrant + BM25, RRF k=60), grade
evidence against the ≥0.3 threshold; on fail, either rewrite-and-retrieve once
or honestly refuse. Generation only runs on passing evidence, followed by the
grounding/citation check and PII guardrail.

## Consequences

- Positive: hallucination class collapses to "refusal" — auditable, safe,
  and a corpus-growth signal.
- Negative: extra grading hop + higher non-answer rate on thin corpora; tuning
  the 0.3 threshold is an ongoing eval task (`eval/`).

## Alternatives

- Vanilla RAG with lower temperature: reduces creativity, not grounding error —
  the model still cites junk confidently.
- Pure rerank thresholding without rewrite: refuses well but never recovers
  from a merely badly-phrased query; CRAG's corrective step earns back recall.
