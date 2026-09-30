"""Dataset integrity guards for eval/**/*.jsonl.

Phase A.5 found that eval/golden/no_answer.jsonl had one pretty-printed
object spanning 22 physical lines. eval/audit_eval_rows.py parses such files
(brace-balanced blocks), but src/maia/eval.py used a naive per-line json.loads,
so the two readers disagreed and three Gate 8 tests died with JSONDecodeError.

These tests keep every file parseable by BOTH readers, so the two can never
silently diverge again.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

# tests/eval/test_dataset_integrity.py -> parents[0]=eval, parents[1]=tests, parents[2]=repo root
EVAL_DIR = pathlib.Path(__file__).resolve().parents[2] / "eval"


def _jsonl_files() -> list[pathlib.Path]:
    return sorted(EVAL_DIR.rglob("*.jsonl"))


def test_eval_dir_contains_jsonl_files() -> None:
    # Guards the two tests below against passing vacuously on an empty glob.
    assert _jsonl_files(), f"no *.jsonl found under {EVAL_DIR}"


@pytest.mark.parametrize("path", _jsonl_files(), ids=lambda p: p.name)
def test_each_physical_line_is_one_json_object(path: pathlib.Path) -> None:
    """Every non-empty physical line must parse standalone.

    This is the naive reader in src/maia/eval.py. A pretty-printed object
    fails here, which is exactly the defect that broke three Gate 8 tests.
    """
    offenders: list[tuple[int, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            offenders.append((lineno, line[:60]))
            continue
        assert isinstance(obj, dict), f"{path.name}:{lineno} is not a JSON object"
    assert not offenders, (
        f"{path.name}: {len(offenders)} line(s) are not standalone JSON objects; "
        f"first offenders: {offenders[:3]}"
    )


@pytest.mark.parametrize("path", _jsonl_files(), ids=lambda p: p.name)
def test_no_duplicate_row_ids(path: pathlib.Path) -> None:
    seen: set[str] = set()
    dupes: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        rid = json.loads(line).get("id")
        if rid is None:
            continue
        if rid in seen:
            dupes.append(f"{rid} (line {lineno})")
        seen.add(rid)
    assert not dupes, f"{path.name}: duplicate row ids: {dupes}"


def test_braced_reader_agrees_with_line_reader() -> None:
    """The two readers in this repo must yield identical row counts.

    eval/audit_eval_rows.read_rows() is brace-balanced and tolerant of
    multi-line objects; src/maia/eval.py is strictly one-object-per-line.
    If both ever return different counts, one of them is measuring a
    different dataset than the other.
    """
    sys.path.insert(0, str(EVAL_DIR))
    from audit_eval_rows import read_rows  # noqa: E402

    for path in _jsonl_files():
        braced = read_rows(path)
        per_line = [
            json.loads(l)
            for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("//")
        ]
        assert len(braced) == len(per_line), (
            f"{path.name}: brace reader saw {len(braced)} rows, "
            f"line reader saw {len(per_line)}"
        )


def test_rejected_rows_stay_in_place_and_are_not_silently_dropped() -> None:
    """A row retired by corpus verification must remain in its file.

    Removing it would make the audit report 'rejected=0' and hide the fact
    that a label was ever contradicted. The file keeps the row; the audit
    classifies it as EXPLICIT_REJECTED and excludes it from metrics.
    """
    no_answer = EVAL_DIR / "golden" / "no_answer.jsonl"
    rows = [json.loads(l) for l in no_answer.read_text(encoding="utf-8").splitlines() if l.strip()]
    rejected = [r for r in rows if r.get("review_status") == "REJECTED"]
    assert rejected, "no_answer.jsonl lost its REJECTED row — corpus verification history erased"
    for row in rejected:
        # A rejected row must explain itself, or the retirement is unauditable.
        assert row.get("rejection_reason"), f"{row.get('id')}: REJECTED without rejection_reason"
        assert row.get("verification"), f"{row.get('id')}: REJECTED without verification evidence"
