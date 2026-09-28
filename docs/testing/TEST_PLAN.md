# Test Plan — MAIA Enterprise RAG & Agent

**Contract:** [`docs/qa/QA_ACCEPTANCE.md`](../../docs/qa/QA_ACCEPTANCE.md).
**Case IDs:** `MAIA-001` → `MAIA-022` in `TEST_CASES.md`.
**Runner:** `pytest`. On disk: 48 files, **369 `def test`** under `tests/` (enumerated 2026-09-27; map, don't duplicate).

## Scope

Hybrid retrieval (Qdrant dense + BM25 + RRF), Evidence Gate `>= 0.3`, citation
correctness, prompt-injection resistance (document + user), LangGraph HITL
(approve/reject/duplicate-approval), transactional outbox, document watcher,
dependency failure (LLM, Qdrant).

## Levels

| Level | What | Where |
|---|---|---|
| Unit | RRF determinism, evidence-gate threshold, citation validation, outbox dedup logic | `tests/test_invariants_property.py`, `test_approval.py` |
| Contract | API/auth shape, gateway wiring, health endpoints | `test_llm_gateway_wiring.py` (16), `test_health_endpoints.py` |
| Property | chunk coverage (no gaps, unique IDs), grounding-score invariants, sanitizer idempotence | `test_invariants_property.py` (5) |
| Race | duplicate approval → single action; concurrent confirms | `test_langgraph_agent.py` (18), `test_e2e_it_outbox.py` |
| Adversarial | injection in doc/user prompt, fake-citation request, conflicting SOPs | `test_injection_adversarial.py` (2), `test_prompt_injection.py` (9), `test_guardrails.py` (9) |
| Live | LLM/Qdrant-backed retrieval quality, guarded by `tests/live_infra.py` | CI with services; graceful skip offline |
| E2E | new SOP file → ingest once → ask → cited answer; dangerous action → HITL → exactly-once effect | `test_watch_docs.py` (8), `test_e2e_it_outbox.py` |

## Environments

| Env | Command | Services | Scope |
|---|---|---|---|
| Offline (dev laptop) | `llm-gateway/.venv/bin/python -m pytest -q` in `llm-gateway/` | none | vendored gateway: 52 tests, no network |
| Offline (app suite) | `pytest tests/` | none | **BLOCKED offline** — needs `pydantic_settings` etc.; CI-ONLY (verified 2026-09-27) |
| CI | `pytest` after `pip install -r requirements.txt` | fakes/fixtures | full 369-test app suite (last-known: badge 128 + 5 Phase-2, UNVERIFIED here) |
| Live-infra | `LIVE_TESTS=1 pytest` | Qdrant container + LM Studio/Ollama | retrieval-quality cases MAIA-001–003, 020–021 |

## Entry / exit criteria

- Entry: fixture corpus frozen; threshold `0.3` pinned in config.
- Exit: P0 100% PASS incl. the two top invariants at unit + integration + E2E.

## Invariants under test

```text
NO EVIDENCE -> NO FACTUAL ANSWER
NO HUMAN APPROVAL -> NO SIDE EFFECT
```
