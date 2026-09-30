# Phase A.5 — MAIA eval dataset baseline repair

Repo: `/Users/mainguyenbinhtan/Downloads/Projects/MAIA`
Base SHA: `9f65912` (audit tag `local-cleanup-2026-09-30` = `98874a9`)
Env: `.e2e-envs/maia` (Python 3.11.15)

## Trigger

Phase A measured `3 failed, 911 passed, 6 skipped` plus 2 collection errors
that aborted the suite. All three failures traced to one file.

## Root cause

Two readers disagreed about the same JSONL file:

- `eval/audit_eval_rows.py::read_rows()` — brace-balanced, **tolerates**
  pretty-printed multi-line objects by design.
- `src/maia/eval.py::evaluate_group()` — naive per-line `json.loads`,
  **does not** tolerate them.

`eval/golden/no_answer.jsonl` contained one pretty-printed object
(`NOANS-009`) spanning 22 physical lines. The audit CLI read it fine; the
Gate 8 evaluation crashed with `JSONDecodeError`. Three tests died on it:
`test_golden_eval.py::test_no_answer_group_has_refusal_accuracy`,
`test_golden_eval.py::test_benchmark_threshold_runs_and_picks`,
`test_threshold_regression.py::test_similarity_threshold_evaluation_runs`.

IMPORTANT: this was NOT data loss. 22 malformed physical lines were 1
recoverable logical object. Forensic parse recovered 9/9 logical records,
0 unrecoverable, 0 duplicate IDs, 0 missing IDs, 0 truncation.

## Corpus verification of the disputed row

`NOANS-009` asserts a carry-over cap is NOT stated in the corpus. Verified
directly against the canonical corpus before any decision:

- Source: `data/enterprise/Leave_Policy.md:18`
- Span: `Chuyển phép (carry over): tối đa **3 ngày** chưa dùng sang năm sau`
- Corpus chunk: `1829fe5aff80_0` in `storage/bm25_corpus_default.json`,
  `tenant_id=default`
- Literal substring hits: `carry over`=2, `Chuyển phép`=1, `sang năm sau`=1,
  `3 ngày`=3

The answer IS present. The label was genuinely wrong, not merely unverified.

## Decision taken, and one taken back

The row was NOT moved to the answerable set and no `ANSW-011` was created.
The repo already ships the correct mechanism: `audit_eval_rows.py` defines
`EXPLICIT_REJECTED`, counts retired rows under `rejected`, and excludes them
from metrics while keeping them in place. `tests/eval/test_audit_cli.py::
TestOutcomeAccounting` locks that behaviour in — "EXPECTED REJECTION IS NOT
FAILURE" — and asserts `NOANS-009` is the rejected row.

So the original design was: keep the row, mark it REJECTED, exclude it from
scored metrics. Deleting the row would have silently erased the evidence
that a label was ever contradicted, and moving it to answerable would have
invented a row ID and provenance that the audit CLI never produced.

## Changes

| File | Change |
|---|---|
| `eval/golden/no_answer.jsonl` | Canonicalised to one-object-per-line JSONL. No field, label, `review_status`, or value changed. |
| `tests/eval/test_dataset_integrity.py` | New. Per-line parse, duplicate-ID, brace-vs-line reader agreement, REJECTED-row retention. |
| `tests/eval/test_audit_cli.py` | `parents[1]` -> `parents[2]`; resolved to `tests/eval` instead of repo root, causing `ModuleNotFoundError: audit_eval_rows`. |
| `tests/test_llama_index_dataplane.py` | `pytest.importorskip("llama_index.core")`. |

### SHA256

```
before  88d61a79f205597c7b15de89da8416a7eeabe7ed1edc664c37af19d3c624e228
after   04f9a61d1054606632e30322f04aaa5a72432b277b85fa9e436a066529a5b301
```

Original preserved at `docs/evidence/phaseA5/no_answer.jsonl.orig`.

## Collection errors

- `audit_eval_rows`: test-isolation bug (wrong `parents` index), fixed.
- `llama_index`: optional opt-in dependency, deliberately absent from
  `requirements.api.txt`, gated behind `LLAMA_INDEX_DATA_PLANE`. Declared
  with `importorskip`, which is a dependency declaration, not a skip to
  green the suite. The adapter stays fully tested wherever installed.

## Integrity test proven to have teeth

Re-pretty-printed `NOANS-009` in place: 4 tests failed
(`test_each_physical_line_is_one_json_object[no_answer.jsonl]`,
`test_no_duplicate_row_ids[no_answer.jsonl]`,
`test_braced_reader_agrees_with_line_reader`,
`test_rejected_rows_stay_in_place_and_are_not_silently_dropped`).
Reverted; 45 passed.

## Results

Targeted Gate 8 tests: **3 passed** (311s).

Full suite, no `--ignore`, no test file excluded:

```
967 passed, 7 skipped, 3 xfailed, 8 warnings in 159.47s
```

Phase A baseline was `3 failed, 911 passed, 6 skipped` + 2 collection errors.

## GATE 8 STATUS: PARTIAL — not VERIFIED

`eval/audit_eval_rows.py --only no_answer`:

```
no_answer   rows=9   usable=0   unusable=8   rejected=1   conflict=0
usable in metrics:           0
unusable (not yet evidence): 8
rejected (expected):         1
```

**Zero no-answer rows are currently usable in metrics.** The eight remaining
rows carry no `review_status`, so they are treated as PENDING at the audit
layer and are excluded from scored metrics. The 1 rejected row is the
`NOANS-009` retirement above.

Green tests therefore do NOT mean the abstention claim is demonstrated. The
suite is green; the no-answer metric is unmeasured. Repairing the reader
restored the ability to measure, and the measurement itself is the open
work.

No refusal-accuracy or abstention number is reported here, because
producing one over 0 usable rows would be reporting a metric of nothing.
