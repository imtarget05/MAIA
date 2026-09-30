"""Regression tests for the eval audit CLI.

The bug these lock down: `only = sys.argv[1]` treated any first argument as a
project name, so `--json` was silently swallowed as a project and the tool
emitted a human report while the caller believed it had asked for JSON.

Every test asserts on exit code AND on stream separation, because the failure
mode was "looks like it worked, stdout just isn't the documented shape".
"""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys

import pytest

# tests/eval/test_audit_cli.py -> parents[0]=eval, parents[1]=tests, parents[2]=repo root.
# The original parents[1]/"eval" resolved to tests/eval, where audit_eval_rows.py
# does not live, so collection failed with ModuleNotFoundError.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "eval"))

from audit_eval_rows import main, parse_args  # noqa: E402


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Invoke main() and capture (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class TestParseArgs:
    def test_default_has_no_only_and_no_json(self):
        assert parse_args([]) == (None, False)

    def test_json_flag_alone(self):
        assert parse_args(["--json"]) == (None, True)

    def test_only_takes_its_value(self):
        assert parse_args(["--only", "no_answer"]) == ("no_answer", False)

    def test_only_before_json(self):
        assert parse_args(["--only", "no_answer", "--json"]) == ("no_answer", True)

    def test_json_before_only_is_order_independent(self):
        assert parse_args(["--json", "--only", "no_answer"]) == ("no_answer", True)

    def test_missing_only_value_is_an_error(self):
        # Must not silently fall back to auditing everything.
        with pytest.raises(SystemExit) as exc:
            parse_args(["--only"])
        assert exc.value.code == 2


class TestJsonMode:
    def test_json_output_is_machine_parseable(self):
        code, out, _ = _run(["--json"])
        assert code == 0
        payload = json.loads(out)  # raises if contaminated
        assert "test_results" in payload

    def test_json_stdout_has_no_human_logs(self):
        _, out, _ = _run(["--json", "--only", "no_answer"])
        # Every non-whitespace character must be part of the JSON document.
        assert out.strip().startswith("{")
        assert out.strip().endswith("}")
        json.loads(out)

    def test_json_flag_is_not_parsed_as_a_project(self):
        """The exact regression: --json must not become the project name."""
        _, out, _ = _run(["--json"])
        payload = json.loads(out)
        # A whole-repo audit, not an empty audit for a project named "--json".
        assert payload["test_results"]["total"] > 1

    def test_denominator_identity_holds(self):
        _, out, _ = _run(["--json"])
        payload = json.loads(out)
        check = payload["denominator_check"]
        assert check["valid"] is True
        assert check["total"] == check["sum_of_categories"]


class TestExitCodes:
    def test_default_invocation_succeeds(self):
        code, _, _ = _run([])
        assert code == 0

    def test_invalid_project_exits_nonzero(self):
        code, _, err = _run(["--only", "does_not_exist"])
        assert code == 2
        assert "available" in err

    def test_invalid_project_in_json_mode_still_json(self):
        code, out, _ = _run(["--json", "--only", "does_not_exist"])
        assert code == 2
        payload = json.loads(out)
        assert "error" in payload

    def test_valid_project_exits_zero(self):
        code, _, _ = _run(["--only", "no_answer"])
        assert code == 0


class TestOutcomeAccounting:
    def test_rejected_is_counted_separately_from_failure(self):
        """EXPECTED REJECTION IS NOT FAILURE.

        NOANS-009 was retired because corpus verification contradicted its
        label. It must appear under `rejected`, never under `failed`.
        """
        _, out, _ = _run(["--json", "--only", "no_answer"])
        payload = json.loads(out)
        results = payload["test_results"]
        assert results["rejected"] >= 1
        assert results["failed"] == 0

    def test_the_retired_row_is_the_carry_over_one(self):
        _, out, _ = _run(["--json", "--only", "no_answer"])
        payload = json.loads(out)
        ids = [r["detail"].split(":")[0] for r in payload["rejected"]]
        assert "NOANS-009" in ids
