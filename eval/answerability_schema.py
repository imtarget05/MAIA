#!/usr/bin/env python3
"""Legacy -> P2 schema normalisation for MAIA eval rows.

SCHEMA NORMALISATION IS NOT GROUND-TRUTH VERIFICATION
----------------------------------------------------
This module answers one question: "what does this row's label MEAN?"

It does NOT answer "is this label CORRECT?"

`expect_refusal=true` tells us the old dataset expected a refusal. It does not
prove that refusing was right against the canonical corpus. NOANS-009 is the
worked example: it asserted no clause states a carry-over cap, while
Leave_Policy.md states "Chuyển phép (carry over): tối đa 3 ngày". A confident
note, mechanically falsified by eval/verify_eval_rows.py.

Two independent fields, never conflated:

    answerability   what the label means
    review_status   whether that label has been verified

THE MAPPER IS PURE. It never writes to the row it is handed, and it NEVER
infers VERIFIED from a legacy label. A row with no review_status stays
unverified; absence is read at the audit layer with .get(), not patched in.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

VALID_ANSWERABILITY = {"ANSWERABLE", "NO_ANSWER", "AMBIGUOUS", "PENDING"}

# Label provenance, reported so a reader can tell a hand-authored label from
# one merely derived from a legacy boolean.
EXPLICIT = "EXPLICIT"
LEGACY_EXPECT_REFUSAL = "LEGACY_EXPECT_REFUSAL"
DEFAULT_PENDING = "DEFAULT_PENDING"
# Explicit label on a row corpus verification has since RETIRED. Surfaced
# separately so a rejection is never silently counted as a normal label.
EXPLICIT_REJECTED = "EXPLICIT_REJECTED"


def map_legacy_answerability(row: dict) -> tuple[str, str]:
    """Return (answerability, source). Pure: does not mutate `row`.

    Raises ValueError on a genuine schema conflict, because silently picking
    one side of a contradiction is how a golden set rots.
    """
    explicit = row.get("answerability")
    has_legacy = "expect_refusal" in row
    legacy = row.get("expect_refusal")

    if explicit is not None:
        if explicit not in VALID_ANSWERABILITY:
            raise ValueError(
                f"Invalid answerability={explicit!r} "
                f"for row id={row.get('id')!r}")

        if has_legacy:
            if not isinstance(legacy, bool):
                raise ValueError(
                    f"expect_refusal must be bool for row "
                    f"id={row.get('id')!r}; got {legacy!r}")

            expected_from_legacy = "NO_ANSWER" if legacy else "ANSWERABLE"

            # AMBIGUOUS/PENDING cannot be represented by a legacy bool, so
            # there is nothing to cross-check.
            if explicit in {"ANSWERABLE", "NO_ANSWER"}:
                if explicit != expected_from_legacy:
                    # A row may be deliberately RETIRED: corpus verification
                    # overrode the legacy expectation and recorded the
                    # contradiction. That is a documented finding, not a
                    # schema bug, so it is reported rather than raised.
                    if row.get("review_status") == "REJECTED":
                        return explicit, EXPLICIT_REJECTED
                    raise ValueError(
                        f"Schema conflict for row id={row.get('id')!r}: "
                        f"answerability={explicit!r}, "
                        f"expect_refusal={legacy!r} "
                        f"(maps to {expected_from_legacy!r})")

        return explicit, EXPLICIT

    if has_legacy:
        if not isinstance(legacy, bool):
            raise ValueError(
                f"expect_refusal must be bool for row "
                f"id={row.get('id')!r}; got {legacy!r}")
        return ("NO_ANSWER" if legacy else "ANSWERABLE"), LEGACY_EXPECT_REFUSAL

    return "PENDING", DEFAULT_PENDING


# --- self-tests -------------------------------------------------------------
# These guard the two ways this mapper could quietly reintroduce the exact bug
# it exists to prevent: a legacy boolean being laundered into verified truth.

def test_legacy_true_maps_to_no_answer() -> None:
    assert map_legacy_answerability({"expect_refusal": True})[0] == "NO_ANSWER"


def test_legacy_false_maps_to_answerable() -> None:
    assert map_legacy_answerability({"expect_refusal": False})[0] == "ANSWERABLE"


def test_missing_label_stays_pending() -> None:
    assert map_legacy_answerability({})[0] == "PENDING"


def test_conflicting_old_and_new_schema_fails() -> None:
    row = {"answerability": "ANSWERABLE", "expect_refusal": True}
    try:
        map_legacy_answerability(row)
    except ValueError:
        return
    raise AssertionError("conflicting schemas must fail")


def test_mapper_never_changes_review_status() -> None:
    row = {"expect_refusal": True, "review_status": "PENDING"}
    before = dict(row)
    answerability, source = map_legacy_answerability(row)
    assert answerability == "NO_ANSWER"
    assert source == LEGACY_EXPECT_REFUSAL
    assert row == before
    assert row["review_status"] == "PENDING"


def test_verified_status_is_not_inferred_from_legacy_label() -> None:
    row = {"expect_refusal": False}
    answerability, _ = map_legacy_answerability(row)
    assert answerability == "ANSWERABLE"
    assert "review_status" not in row


def test_ambiguous_is_preserved_not_flattened() -> None:
    assert map_legacy_answerability({"answerability": "AMBIGUOUS"})[0] == "AMBIGUOUS"


def test_invalid_answerability_raises() -> None:
    try:
        map_legacy_answerability({"answerability": "MAYBE"})
    except ValueError:
        return
    raise AssertionError("invalid answerability must fail")


def test_rejected_row_reports_its_own_provenance() -> None:
    """A corpus-verified retirement is a documented finding, not a crash."""
    row = {"answerability": "ANSWERABLE", "expect_refusal": True,
           "review_status": "REJECTED"}
    label, source = map_legacy_answerability(row)
    assert label == "ANSWERABLE"
    assert source == EXPLICIT_REJECTED
    assert row["review_status"] == "REJECTED"


def test_rejected_status_is_not_self_granting() -> None:
    """REJECTED must never be reported as a normal EXPLICIT label."""
    row = {"answerability": "ANSWERABLE", "expect_refusal": True,
           "review_status": "REJECTED"}
    _, source = map_legacy_answerability(row)
    assert source != EXPLICIT


def test_conflict_without_rejected_still_raises() -> None:
    """Retiring a row must be explicit; an unmarked conflict is still an error."""
    row = {"answerability": "ANSWERABLE", "expect_refusal": True}
    try:
        map_legacy_answerability(row)
    except ValueError:
        return
    raise AssertionError("unmarked schema conflict must fail")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def run_selftests() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001 - report, do not hide
            print(f"  FAIL {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} self-tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_selftests())
