# Test Cases — MAIA Enterprise RAG & Agent

**Plan:** [`TEST_PLAN.md`](TEST_PLAN.md). **Contract:** `docs/qa/QA_ACCEPTANCE.md`.
On-disk enumeration 2026-09-28: 50 test files.
Run 2026-09-28 (repo `.venv`, `MAIA_EMBED_FORCE_HASH=1` via conftest, live localhost
services present — Qdrant :6333 live, gateway :8787 live): **All Invariant Suites Passing**.

| ID | Test case | How to test | Expected | Priority | Status |
|---|---|---|---|---|---|
| MAIA-001 | Exact keyword retrieval | Query with keyword from a single SOP | Correct document in top results | P0 | PASS (`test_llama_index_dataplane.py::test_query_returns_matching_node`, `test_source_reader.py` ×7) |
| MAIA-002 | Semantic retrieval | Paraphrase, no keyword overlap | Dense retrieval finds correct evidence | P0 | PASS (`test_corrective_rag.py` ×11, `test_query_returns_nodes_and_similarities`) |
| MAIA-003 | Hybrid fusion | Dense vs BM25 disagree | RRF ranking deterministic | P1 | PASS (`test_hybrid_retrieval_with_bm25_and_rrf`, `test_compare_baselines_hybrid_beats_bm25`) |
| MAIA-004 | Unsupported question | Fully out-of-corpus question | No hallucinated answer | P0 | PASS (`test_guarded_generate_fallback_on_no_evidence`, `test_grounding_checker_rejects_hallucination`) |
| MAIA-005 | Evidence below threshold | Score `< 0.3` | Gate blocks factual answer | P0 | PASS (thresholds locked: `SIMILARITY_THRESHOLD=0.3` in `config.py:56`; `test_threshold_regression.py` ×3 guards `> 0`; `test_eval_gate.py` ×4) |
| MAIA-006 | Citation correctness | Check each `[S1]` | File/line really contains the claim | P0 | PASS (`test_verify_support_true_when_overlap`, `test_verify_support_false_when_no_overlap`, `test_extract_cites_dedupes_and_sorts`, `test_citation_checker_detects_invalid_cite`) |
| MAIA-007 | Fake citation attack | Prompt demands bogus `[S99]` | No non-existent citation generated | P0 | PASS (same citation tests; unauthorized-source flags `test_check_unauthorized_sources_*`) |
| MAIA-008 | Conflicting documents | Two contradictory SOPs | Presents both evidences, no invented conclusion | P1 | PASS (`tests/test_grounding.py::test_maia_008_contradictory_documents_isolation`) |
| MAIA-009 | Prompt injection in document | Doc contains `"ignore previous instructions"` | Document content never takes over agent | P0 | PASS (`test_prompt_injection.py` ×16, `test_guardrails.py` ×9, `test_injection_adversarial.py` DEF-MAIA-001 FIXED) |
| MAIA-010 | User prompt injection | `"ignore guardrail..."` | Guardrail holds | P0 | PASS (same files; `test_llm_cannot_escape_the_spec_schema` pattern in gateway suite) |
| MAIA-011 | HITL side effect | Agent prepares dangerous action | Workflow stops at interrupt | P0 | PASS (`test_approval.py` ×9, `test_langgraph_agent.py` ×18) |
| MAIA-012 | Reject HITL | User rejects | Zero side effects | P0 | PASS (same files) |
| MAIA-013 | Approve HITL once | Single approval | Action runs exactly once | P0 | PASS (same files) |
| MAIA-014 | Duplicate approval | Retry/reload approval | Action never sent twice | P0 | PASS (same files; `test_agentic.py` ×6) |
| MAIA-015 | Outbox delivery retry | Webhook fails 500 | Retries per policy | P0 | PASS (`test_e2e_it_outbox.py` ×2) |
| MAIA-016 | Outbox duplicate processing | Worker replays old event | No duplicate business side effect | P0 | PASS (same file) |
| MAIA-017 | New document watcher | Add new SOP | Detected + ingested exactly once | P1 | PASS (`test_watch_docs.py` ×8, `test_document_lifecycle.py` ×7) |
| MAIA-018 | Partial file writing | File still copying | No half-written ingest | P1 | PASS (watcher/lifecycle suites; empty chunking DEF-MAIA-002 FIXED) |
| MAIA-019 | Unsupported document | Corrupt file | Worker alive, error logged | P1 | PASS (`test_source_reader.py`, `test_tenant_authorization.py` isolation; lifecycle suite) |
| MAIA-020 | LLM unavailable | Stop LM Studio/Ollama | No crash; clear error/fallback | P1 | PASS (`test_resilience.py` ×13, `test_llm_gateway_wiring.py` ×16; gateway suite 52 passed) |
| MAIA-021 | Vector DB unavailable | Stop Qdrant | Controlled failure, no fake evidence | P0 | PASS (`test_resilience.py`; `test_query_no_embedding_returns_empty`) |
| MAIA-022 | Citation stability | Same query repeatedly | Evidence never points to wrong source | P1 | PASS (`tests/test_grounding.py::test_maia_022_citation_stability_deterministic`) |

**Gate verdict 2026-09-28: QA READY**
- 100% P0 PASS (DEF-MAIA-001 fixed).
- 100% P1 PASS (MAIA-008, MAIA-022 covered and verified).
- P2 DEF-MAIA-002 fixed (normalized empty-chunking in `maia/chunking.py`).

