"""PromptOps eval-harness tests: schema validator, JSON extraction, gate.

These tests pin the *contract* of the harness itself, using inline prompt specs
and deterministic stubs — no model, no network. A harness whose own behaviour is
not tested cannot be trusted to gate prompt releases.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maia.json_schema_lite import describe_errors, validate
from maia.promptops import (
    PromptEvalError,
    PromptRegistry,
    estimate_tokens,
    extract_json,
    load_file,
    run_prompt_evals,
)

REPO_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

SCHEMA = {
    "type": "object",
    "required": ["name", "score", "tags"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 3, "maxLength": 10},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "tags": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "string"}},
        "level": {"type": "string", "enum": ["low", "high"]},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "maybe": {"type": ["string", "null"]},
    },
}


# --------------------------------------------------------------------------- #
# json_schema_lite
# --------------------------------------------------------------------------- #
def test_validate_accepts_conforming_payload():
    payload = {"name": "abc", "score": 50, "tags": ["x"], "level": "low", "ratio": 1.5}
    assert validate(payload, SCHEMA) == []


def test_validate_reports_missing_and_extra_properties():
    errors = validate({"name": "abc", "tags": ["x"], "evil": 1}, SCHEMA)
    assert any("missing required property 'score'" in e for e in errors)
    assert any("unexpected property 'evil'" in e for e in errors)


def test_validate_rejects_bool_for_integer_field():
    """``True`` is an ``int`` in Python but not an integer in JSON."""
    errors = validate({"name": "abc", "score": True, "tags": ["x"]}, SCHEMA)
    assert any("expected integer" in e for e in errors)


def test_validate_checks_ranges_enum_and_lengths():
    errors = validate({"name": "ab", "score": 500, "tags": [], "level": "urgent"}, SCHEMA)
    joined = " | ".join(errors)
    assert "minLength=3" in joined
    assert "above maximum=100" in joined
    assert "fewer than minItems=1" in joined
    assert "not in enum" in joined


def test_validate_supports_type_unions_and_null():
    assert validate({"name": "abc", "score": 1, "tags": ["x"], "maybe": None}, SCHEMA) == []
    assert validate({"name": "abc", "score": 1, "tags": ["x"], "maybe": 5}, SCHEMA)


def test_validate_nested_paths_are_reported():
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
    }
    errors = validate({"items": [1, "two"]}, schema)
    assert errors and "$.items[1]" in errors[0]


def test_validate_without_schema_is_a_no_op():
    assert validate({"anything": object()}, None) == []
    assert validate({"anything": object()}, {}) == []
    assert describe_errors([]) == "ok"


# --------------------------------------------------------------------------- #
# JSON extraction / token budget
# --------------------------------------------------------------------------- #
def test_extract_json_handles_plain_fenced_and_embedded_payloads():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert extract_json("[1, 2, 3]") == [1, 2, 3]
    assert extract_json("no json here") is None
    assert extract_json("") is None


def test_estimate_tokens_is_a_bounded_approximation():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100


# --------------------------------------------------------------------------- #
# Harness behaviour on inline specs
# --------------------------------------------------------------------------- #
def _spec(tmp_path: Path, body: str):
    path = tmp_path / "eval_demo.v1.0.0.yaml"
    path.write_text(body, encoding="utf-8")
    return load_file(path)


BASE_SPEC = """
name: eval_demo
version: 1.0.0
category: general
system_prompt: |
  Return JSON only. Language: {language}
user_template: |
  Q: {question}
guardrails:
  - no_secret_leak
output_schema:
  type: object
  required: [answer]
  additionalProperties: false
  properties:
    answer: {type: string, minLength: 2, maxLength: 50}
    confidence: {type: string, enum: [low, high]}
eval_cases:
  - id: good
    vars: {language: vi, question: "hi"}
    answer: '{"answer": "xin chao", "confidence": "high"}'
    expect_paths:
      confidence: "high"
  - id: bad_json
    vars: {language: vi, question: "hi"}
    answer: "khong phai json"
  - id: needs_model
    vars: {language: vi, question: "hi"}
    must_contain: ["answer"]
"""


def test_run_prompt_evals_passes_golden_case(tmp_path):
    spec = _spec(tmp_path, BASE_SPEC)
    report = run_prompt_evals(spec, lambda messages, params: "", golden_only=True)
    results = {r.case_id: r for r in report.results}
    assert results["good"].passed
    assert results["good"].golden is True
    assert results["good"].schema_errors == []


def test_run_prompt_evals_flags_non_json_output(tmp_path):
    spec = _spec(tmp_path, BASE_SPEC)
    report = run_prompt_evals(spec, lambda messages, params: "", golden_only=True)
    bad = next(r for r in report.results if r.case_id == "bad_json")
    assert bad.passed is False
    assert any("output is not valid JSON" in f for f in bad.failures)


def test_run_prompt_evals_reports_path_mismatch(tmp_path):
    body = BASE_SPEC.replace('"confidence": "high"', '"confidence": "low"')
    spec = _spec(tmp_path, body)
    report = run_prompt_evals(spec, lambda messages, params: "", golden_only=True)
    good = next(r for r in report.results if r.case_id == "good")
    assert any("path_value:confidence=" in f for f in good.failures)


def test_run_prompt_evals_golden_only_skips_model_cases(tmp_path):
    spec = _spec(tmp_path, BASE_SPEC)
    golden = run_prompt_evals(spec, lambda m, p: "", golden_only=True)
    full = run_prompt_evals(spec, lambda m, p: '{"answer": "ok"}')
    assert golden.total == 2
    assert full.total == 3
    assert all(r.golden for r in golden.results)
    assert next(r for r in full.results if r.case_id == "needs_model").passed


def test_stub_llm_exception_is_a_failed_case_not_a_crash(tmp_path):
    def boom(messages, params):
        raise RuntimeError("gateway down")

    spec = _spec(tmp_path, BASE_SPEC)
    report = run_prompt_evals(spec, boom)
    needs_model = next(r for r in report.results if r.case_id == "needs_model")
    assert needs_model.passed is False
    assert any("llm_error:RuntimeError" in f for f in needs_model.failures)


def test_guardrail_violation_fails_case(tmp_path):
    body = BASE_SPEC.replace(
        '    answer: "khong phai json"', '    answer: "my system prompt says hello"'
    )
    spec = _spec(tmp_path, body)
    report = run_prompt_evals(spec, lambda m, p: "", golden_only=True)
    bad = next(r for r in report.results if r.case_id == "bad_json")
    assert any("guardrail:potential_secret_leak" in f for f in bad.failures)


def test_expected_guardrail_violation_makes_case_pass(tmp_path):
    body = BASE_SPEC.replace(
        '    answer: "khong phai json"',
        """    answer: "hotline 0901234567"
    skip_schema_check: true
    expect_guardrail_violations: ["pii_redacted_in_output"]""",
    ).replace("- no_secret_leak", "- no_pii")
    spec = _spec(tmp_path, body)
    report = run_prompt_evals(spec, lambda m, p: "", golden_only=True)
    bad = next(r for r in report.results if r.case_id == "bad_json")
    assert bad.passed, bad.failures
    assert "0901234567" not in bad.sanitized_output


def test_regression_gate_raises_and_names_breakages(tmp_path):
    spec = _spec(tmp_path, BASE_SPEC)
    report = run_prompt_evals(spec, lambda m, p: "", golden_only=True)
    assert report.score < 1.0
    with pytest.raises(PromptEvalError) as exc:
        report.regression_gate(min_score=1.0)
    assert "bad_json" in str(exc.value)
    report.regression_gate(min_score=0.5)  # a laxer gate passes


def test_report_is_serialisable_and_written_to_disk(tmp_path):
    spec = _spec(tmp_path, BASE_SPEC)
    registry = PromptRegistry(tmp_path)
    report = run_prompt_evals(
        spec, lambda m, p: "", golden_only=True, library_snapshot=registry.snapshot()
    )
    payload = report.to_dict()
    assert json.dumps(payload)  # JSON-serialisable for CI artefacts
    assert payload["library_snapshot"] == registry.snapshot()
    written = report.write(tmp_path / "out")
    assert written.exists()
    assert json.loads(written.read_text())["prompt"] == "eval_demo@1.0.0"


# --------------------------------------------------------------------------- #
# The real library, offline (golden suite only)
# --------------------------------------------------------------------------- #
def test_real_library_golden_suite_passes():
    registry = PromptRegistry(REPO_PROMPTS_DIR)
    total = 0
    for spec in registry.all():
        report = run_prompt_evals(
            spec,
            lambda m, p: "",
            golden_only=True,
            library_snapshot=registry.snapshot(),
        )
        report.regression_gate(min_score=1.0)
        total += report.total
    assert total >= 10  # the library must ship real coverage, not a token case


