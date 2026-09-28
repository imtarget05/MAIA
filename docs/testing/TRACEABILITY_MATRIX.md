# Traceability Matrix — MAIA

`Requirement → Business Rule → Test Case → Automated Test → Execution Evidence`
Run 2026-09-27: 518 passed, 1 failed, 0 skipped (`evidence/2026-09-27-pytest-full.log`).

| Requirement | Business Rule | Test Case | Automated Test (exact nodes, all green unless noted) | Evidence |
|---|---|---|---|---|
| REQ-RET-001: retrieval finds the right evidence | keyword + semantic + hybrid RRF | MAIA-001, MAIA-002, MAIA-003 | `test_llama_index_dataplane.py::test_query_returns_matching_node`, `::test_hybrid_retrieval_with_bm25_and_rrf`; `test_corrective_rag.py` (11); `test_loops.py::test_compare_baselines_hybrid_beats_bm25` | full log |
| REQ-GATE-001: no evidence, no factual answer | threshold 0.3 locked; below-threshold → fallback, never hallucination | MAIA-004, MAIA-005 | `test_loops.py::test_guarded_generate_fallback_on_no_evidence`, `::test_grounding_checker_rejects_hallucination`; `test_threshold_regression.py` (3, locks `SIMILARITY_THRESHOLD`/`AGENT_EVIDENCE_THRESHOLD > 0`); `test_eval_gate.py` (4) | full log |
| REQ-CIT-001: citations are real | every cite resolves to supporting text; fakes rejected | MAIA-006, MAIA-007 | `test_grounding.py::test_verify_support_true_when_overlap`, `::test_verify_support_false_when_no_overlap`, `::test_citation_checker_detects_invalid_cite` (`test_loops.py`), `::test_check_unauthorized_sources_*` | full log |
| REQ-INJ-001: injection never takes over | doc/user override attempts fail closed | MAIA-009, MAIA-010 | `test_prompt_injection.py` (16), `test_guardrails.py` (9). Dead module: `test_injection_adversarial.py` → DEF-MAIA-001 | full log + defect |
| REQ-HITL-001: no approval, no side effect | stop at interrupt; reject = zero effects; approve = exactly once; duplicate approval safe | MAIA-011 → MAIA-014 | `test_approval.py` (9), `test_langgraph_agent.py` (18), `test_agentic.py` (6) | full log |
| REQ-OUT-001: outbox exactly-once effect | retry per policy; replay causes no duplicate side effect | MAIA-015, MAIA-016 | `test_e2e_it_outbox.py` (2) | full log |
| REQ-ING-001: ingest is safe and complete | new doc once; no half/empty/corrupt ingest kills worker | MAIA-017, MAIA-018, MAIA-019 | `test_watch_docs.py` (8), `test_document_lifecycle.py` (7), `test_source_reader.py` (7). Edge open: empty-input chunk differs by backend → DEF-MAIA-002 (1 red test) | full log (1 failed) |
| REQ-DEP-001: dependencies fail controlled | LLM/Qdrant down → clear error, no fake evidence | MAIA-020, MAIA-021 | `test_resilience.py` (13), `test_llm_gateway_wiring.py` (16), `test_llama_index_dataplane.py::test_query_no_embedding_returns_empty`. Live localhost (Qdrant :6333, gateway :8787) reachable during run | full log |
| REQ-GAP-001: contradictions surfaced, not invented | conflicting SOPs presented with evidence | MAIA-008 | **none** → DEF-MAIA-003 (OPEN) | — |
| REQ-GAP-002: citations stable across repeats | same query → same sources | MAIA-022 | **none** → DEF-MAIA-004 (OPEN) | — |
