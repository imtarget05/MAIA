# Business Impact — MAIA (Intelligent RAG Knowledge Platform)

Domain facts: enterprise internal Q&A over HR/IT/Security policy corpus with
zero-hallucination evidence gate (≥0.3), hybrid Dense+BM25→RRF retrieval,
citation grounding ([S1]…), and HITL approval for side-effect actions.
See `README.md`, `docs/spec.md`.

## Problem (cost of status quo)

Policy questions (leave, IT tickets, security) are answered by humans searching
wikis/chat threads. Each lookup costs ~15 min of staff time, answers vary by
who is asked, and unactioned requests (leave/Tickets) wait in queues.
Hallucinated answers are the tail risk: wrong policy cited with confidence.

## Solution (what the system does)

`Find → Understand → Cite → Act`: hybrid retrieval (Qdrant dense + BM25 sparse,
RRF k=60) → evidence gate → cross-encoder rerank → grounded generation with
citation check → PII redaction → HITL interrupt for actions (`/actions/confirm`).

## Impact

| Metric | Before | After | How measured |
|---|---|---|---|
| MTTR policy lookup | 15 min | 30 s | ESTIMATE — plan target; pending eval gate (`eval/dataset.jsonl`, 73 golden cases). Not a production measurement. |
| Grounded-answer rate (CRAG-style) | 64% baseline | 92% | ESTIMATE — pending `python -m maia.eval` gate; not measured here. |
| Chunking micro-op (200 iters, local) | — | mean 6.91 ms, p95 12.68 ms | MEASURED by `scripts/bench_maia.py` (mode `maia.chunking.split_documents`, PYTHONPATH=src), this machine 2026-09-27. Machine-dependent; re-run before quoting. |

No other number in this file is a production measurement.

## Guardrails / SLO links

- Evidence gate ≥0.3 + honest refusal; grounding/citation check; 2-layer PII scan.
- SLOs: `observability/slo.yaml` (`maia-grounding-score` target 0.95, `pii-violation-rate` 0).
- Live metrics: `GET /metrics` on the MAIA API; gateway telemetry via llm-gateway `/metrics`.

## Reproduce

```bash
cd MAIA
PYTHONPATH=src python3 scripts/bench_maia.py
MAIA_EMBED_FORCE_HASH=1 python -m pytest tests/ -q
PYTHONPATH=src python -m maia.eval eval/dataset.jsonl   # eval gate (ESTIMATE source)
```
