# Defect Report — MAIA

| ID | Severity | Title | Repro | Evidence | Status |
|---|---|---|---|---|---|
| DEF-MAIA-001 | P0 | `tests/test_injection_adversarial.py` import mismatch (`AgentDecision`, `MaiaAgent`). | `pytest --collect-only` | `tests/test_injection_adversarial.py` refactored to test `EnterpriseAgent` & `InputGuardrail` | FIXED (verified 2 passed, 1 skip live) |
| DEF-MAIA-002 | P2 | `split_documents('')` returned empty chunk instead of `[]` with `llama-index-core`. | `pytest tests/test_invariants_property.py` | `src/maia/chunking.py` updated with empty string pre/post filtering | FIXED (verified 5/5 passed) |
| DEF-MAIA-003 | P1 | Missing conflicting documents test (MAIA-008). | Contract audit | `tests/test_grounding.py::test_maia_008_contradictory_documents_isolation` | FIXED (verified passed) |
| DEF-MAIA-004 | P1 | Missing citation stability test (MAIA-022). | Contract audit | `tests/test_grounding.py::test_maia_022_citation_stability_deterministic` | FIXED (verified passed) |

## Lifecycle

`OPEN → FIXED` (with re-test evidence) or `OPEN → MITIGATED` (workaround +
root-cause tracking ID) or `→ WONTFIX` (justification required for P0/P1).
Every defect links the failing case ID from `TEST_CASES.md`.

