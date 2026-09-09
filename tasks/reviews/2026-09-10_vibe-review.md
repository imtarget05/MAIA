# Vibe Review — Patch Weaknesses Full (WS1–WS8)

**Date**: 2026-09-10  
**Mode**: PLAN MODE → review/verify existing working-tree state  
**Reviewer**: autonomous (Kilo CLI)  
**Skills consulted**: `requesting-code-review/SKILL.md`, `verification-before-completion/SKILL.md` (Vide coding)

## Verdict: CONDITIONAL PASS (tests green; lint/typecheck/CI have issues)

Functional implementation verified and all 277 tests pass. However, lint and
typecheck have **new** errors introduced by the work, the CI config references a
missing test file, and a stray artifact file exists. Not yet commit-safe.

---

## Checklist vs actual state

| WS | Checklist | Actual state | Verified |
|----|-----------|-------------|----------|
| WS1 | [x] committed tests + pin deps | Tests exist on disk (untracked), 42 new tests pass. Deps pinned in requirements.txt. NOT committed to git. | ✅ tests ✅ pin ❌ commit |
| WS2 | [ ] LlamaIndex hybrid + flag | `LLAMA_INDEX_DATA_PLANE=True` in config.py:46. `_llama_index_retrieve()` in langgraph_agent.py:149 implements dense(LlamaIndex)+BM25+RRF k=60. test_hybrid_retrieval passes. | ✅ |
| WS3 | [ ] reranker/SSE | requirements-rerank.txt (sentence-transformers+torch opt-in). SSE uses stable `graph.stream(stream_mode="updates")` (api.py:895). test_agent_chat_stream_sse_resume_after_hitl passes. | ✅ |
| WS4 | [ ] eval expansion + 2-mode | contact_usability.jsonl=15 rows (was 8). paraphrase.jsonl=15 rows (was 8). Manifest `mode` field records hash\|fastembed. | ✅ |
| WS5 | [ ] guardrails merge | OutputValidator is deprecated alias of OutputGuardrail (guardrails.py:250). `is_action_response=True` only at agent.py:422. No dead `is_action_response=False` paths. | ✅ |
| WS6 | [ ] untangle stream/CRAG/LTM | `import maia.agent.agent` does NOT load maia.stream (verified). reliability_loop.py:59 lazy import inside function. CRAG/LTM behind flags (default off). Config flat-only. | ✅ |
| WS7 | [ ] run manifest | eval_manifest.py: build_manifest + write_manifest. CLI `--manifest DIR`. Manifest includes run_id, git_sha, mode, thresholds, flags, metrics. test_eval_manifest.py passes (2 tests). | ✅ |
| WS8 | [ ] JD-MAPPING docs | docs/JD-MAPPING.md exists (3484 bytes), maps JD reqs ↔ files + verify commands + 3-min demo script. | ✅ |

**Key finding**: The checklist marks only WS1 done, but all WS1–WS8 are actually
**implemented and verified in the working tree** (uncommitted). The checklist is
outdated relative to the actual code state.

---

## Commands run + evidence

| Command | Result | Evidence |
|---------|--------|----------|
| `MAIA_EMBED_FORCE_HASH=1 MAIA_MODE=mock pytest tests/ -q` | **277 passed, 2 skipped, 0 failed** | Full offline suite green |
| `pytest tests/test_langgraph_agent.py tests/test_llama_index_dataplane.py tests/test_agent_chat_api.py -v` | **42 passed** | 3 new test files |
| `pytest tests/test_contact_usability.py -v` | **3 passed** | G-04-FU2 contact usability |
| `pytest tests/test_eval_manifest.py tests/test_guardrails.py -v` | **2 + 8 = 10 passed** | WS5 + WS7 |
| `ruff check src/` | **368 errors** (245 pre-existing + 123 new) | CI lint job would fail |
| `pyright src/` | **156 errors** (139 pre-existing + 17 new) | CI typecheck job would fail |
| `ruff check <5 new source files>` | 26 errors (I001, BLE001, UP045, F401, S110, PLE2515) | Style issues, not functional |
| `ruff check <4 new test files>` | 22 errors (I001, F811, F401, S110, BLE001, SIM102) | Style issues |
| `python3 -c "import maia.agent.agent; ..."` | **PASS**: no stream modules at import-time | WS6 lazy-import verified |
| `grep is_action_response src/maia/` | Only `True` at agent.py:422; default `False` in signature | WS5 no dead paths |
| `python3 -m maia.eval --group contact_usability --top-k 3` | Runs, produces results (hash mode → conditional metric) | CLI gates work |

### Lint/typecheck breakdown (new files only)

**src/maia/agent/langgraph_agent.py** (15 ruff, 1 pyright):
- I001: import block unsorted (lines 30, 159, 206, 522)
- RUF022: `__all__` not sorted (line 45)
- UP045: `Optional[str]` → `str | None` (lines 60, 61, 78, 79)
- BLE001: blind `except Exception` (lines 197, 225, 287, 316, 396)
- S110: `try/except/pass` (line 287)
- **pyright:543**: `config` dict type mismatch on `g.invoke()` — type annotation issue, runtime works

**src/maia/llamaindex_store.py** (6 ruff):
- UP035: `typing.List` deprecated (line 23)
- F401: unused `Optional` import (line 23)
- UP006: `List` → `list` (line 64)
- BLE001: blind except (line 89)
- S110: try/except/pass (line 89)

**src/maia/eval_manifest.py** (3 ruff):
- BLE001: blind except (lines 34, 46)
- UP017: `datetime.UTC` alias (line 56)

**src/maia/loops/guardrails.py** (1 ruff):
- PLE2515: zero-width-space literal should be `"\u200b"` (line 120) — functional risk if file is re-encoded

**src/maia/loops/pii.py** (1 ruff):
- BLE001: blind except (line 76)

### Pre-existing vs new

| Metric | Committed (HEAD) | Working tree | Delta (new) |
|--------|:---:|:---:|:---:|
| ruff errors (src/) | 245 | 368 | +123 |
| pyright errors (src/) | 139 | 156 | +17 |

---

## CI config issues (NEW — introduced by CI rewrite)

The `.github/workflows/ci.yml` was rewritten in this work. New issues:

1. **`retrieval-regression` job** (line 200) runs `pytest tests/test_retrieval_regression.py -v`
   but **that file does not exist** in the repo. CI job would fail.
2. **`unit-tests` job** (line 36) runs `pip install -r requirements.txt` then `pytest`
   but **pytest is not in requirements.txt** (old CI had `pip install pytest`
   separately). CI job would fail on import.
3. **`lint-and-typecheck` job** runs `ruff check src/` + `pyright src/` — would fail
   with 368/156 errors.
4. **`retrieval-regression` and `eval-manifest` jobs** reference `tests/test_golden_eval.py`
   and `tests/test_threshold_regression.py` which exist (untracked) — OK if committed.

---

## Stray file

**CRITICAL (cleanup)**: A malformed file named `` `tu 2>&1 | tail -5,` `` exists in
repo root. It contains the captured output of a botched shell command
(`tail: illegal option -- -5, cd`). This is not a valid project file and must be
removed before commit.

---

## Residual risk

| Risk | Severity | Notes |
|------|----------|-------|
| Lint/typecheck errors | Medium | 123 ruff + 17 pyright new errors. CI lint job already red pre-existing (245/139). New files should be cleaned before commit. |
| Missing CI test file | Medium | `test_retrieval_regression.py` referenced in CI but absent. Job fails. |
| CI missing pytest install | Medium | `unit-tests` job doesn't install pytest. Job fails. |
| Stray file | Low | `` `tu 2>&1 | tail -5,` `` — cleanup, no code impact. |
| pyright:543 type mismatch | Low | Runtime works (test_resume_from_approval passes); type annotation should be `RunnableConfig` |
| guardrails:120 zero-width-space | Low | Literal ZWSP in source; PLE2515 suggests `\u200b` — risk if editor re-encodes |
| Tests untracked | Medium | 136 files uncommitted — no git history |

---

## Recommendations

1. **Remove stray file**: `` rm "tu 2>&1 | tail -5," ``
2. **Fix CI**: add `pip install pytest` to `unit-tests` job; create or remove `test_retrieval_regression.py`
3. **Lint cleanup**: run `ruff check --fix` on new files to fix auto-fixable issues (I001, F401, UP006/035/045)
4. **Typecheck**: fix `langgraph_agent.py:543` config type annotation
5. **Commit**: stage all 136 untracked/modified files with a descriptive commit
6. **Re-run**: `MAIA_EMBED_FORCE_HASH=1 MAIA_MODE=mock pytest tests/ -q` after fixes

---

## Rollback

If this work needs to be reverted before commit:
```bash
git stash push -m "vibe-review-2026-09-10" -- src/ tests/ docs/JD-MAPPING.md .github/workflows/ci.yml requirements.txt src/maia/config.py
# Then restore from commit once fixes are applied
git stash pop
```
No commits have been made — all changes are in the working tree.
