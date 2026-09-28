"""Prompt evaluation harness — deterministic, offline prompt regression gate.

Scope discipline (this is what makes the gate trustworthy):

* The harness measures **contract compliance**: JSON-schema validity, required
  and forbidden substrings, exact JSON-path values, guardrail violations, and
  latency/token budgets. It does *not* judge writing quality — that needs a human
  rubric or an LLM judge, a separate and explicitly-flagged exercise. Mixing the
  two produces a gate that flickers and then gets disabled.
* It runs **without a model**: the caller injects any
  ``Callable[[messages, parameters], str]``. In CI that is a deterministic stub;
  against a real gateway it is the production adapter. Same harness, no branching.
* Every report carries the prompt's ``content_hash`` and a full library snapshot,
  so "the suite passed" always answers *for which prompt text*.

``estimate_tokens`` is a deliberate approximation (≈4 chars/token): it exists to
catch an order-of-magnitude regression offline. Real token accounting stays in
the llm-gateway telemetry.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..json_schema_lite import describe_errors, validate
from .guardrails import check_output
from .models import EvalCase, PromptSpec
from .render import render

__all__ = [
    "EvalCaseResult",
    "PromptEvalError",
    "PromptEvalReport",
    "estimate_tokens",
    "extract_json",
    "run_prompt_evals",
]

LLMCallable = Callable[[list[dict[str, str]], dict[str, Any]], str]

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>[\s\S]*?)```", re.IGNORECASE)
_PATH_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\.(\d+)")


class PromptEvalError(AssertionError):
    """Raised by the regression gate when a prompt eval suite does not pass."""


def estimate_tokens(text: str) -> int:
    """≈4 characters per token — a budget guard, not a billing figure."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def extract_json(text: str) -> Any | None:
    """Parse a model answer as JSON, tolerating a ```json fence.

    Returns ``None`` when nothing parses — the caller turns that into a schema
    failure, which is the honest outcome for "the model did not honour the
    structured-output contract".
    """
    if not text:
        return None
    candidate = text.strip()
    fence = _JSON_FENCE_RE.search(candidate)
    if fence:
        candidate = fence.group("body").strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _get_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Resolve ``a.b[0].c`` / ``a.0.c`` against parsed JSON.

    Numeric segments are accepted with either syntax because both appear in eval
    specs and quietly failing on ``.0.`` would make a correct prompt look broken.
    """
    current = payload
    for match in _PATH_RE.finditer(path):
        key, bracket_index, dot_index = (
            match.group(1),
            match.group(2),
            match.group(3),
        )
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
        else:
            if not isinstance(current, list):
                return False, None
            i = int(bracket_index if bracket_index is not None else dot_index)
            if i >= len(current) or i < -len(current):
                return False, None
            current = current[i]
    return True, current


@dataclass
class EvalCaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    latency_ms: int = 0
    output_tokens: int = 0
    output: str = ""
    sanitized_output: str = ""
    schema_errors: list[str] = field(default_factory=list)
    golden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            "passed": self.passed,
            "golden": self.golden,
            "failures": list(self.failures),
            "latency_ms": self.latency_ms,
            "output_tokens": self.output_tokens,
            "schema_errors": list(self.schema_errors),
        }


@dataclass
class PromptEvalReport:
    ref: str
    content_hash: str
    results: list[EvalCaseResult]
    model: str = ""
    started_at: str = ""
    duration_ms: int = 0
    library_snapshot: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def score(self) -> float:
        return 1.0 if not self.results else self.passed / len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.ref,
            "content_hash": self.content_hash,
            "model": self.model,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "score": round(self.score, 4),
            "cases": [r.to_dict() for r in self.results],
            "library_snapshot": dict(sorted(self.library_snapshot.items())),
        }

    def write(self, directory: str | Path) -> Path:
        """Persist the report as JSON (CI artefact / audit evidence)."""
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_ref = self.ref.replace("@", "_").replace(".", "-")
        path = target_dir / f"prompt_eval_{safe_ref}_{stamp}.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def regression_gate(self, min_score: float = 1.0) -> None:
        """Raise unless ``score >= min_score``; used by CI and ``--strict`` runs."""
        if self.score + 1e-9 < min_score:
            breakages = [
                f"{r.case_id}: {'; '.join(r.failures)}"
                for r in self.results
                if not r.passed
            ]
            raise PromptEvalError(
                f"prompt eval gate failed for {self.ref} "
                f"(score {self.score:.2f} < {min_score:.2f}): " + " | ".join(breakages)
            )


def _check_case(spec: PromptSpec, case: EvalCase, llm: LLMCallable) -> EvalCaseResult:
    """Run one case and report every failure it produced (not just the first).

    Reporting all failures at once matters for prompt debugging: when a model
    drops the JSON contract, the schema error, the missing substring and the
    token overshoot are one root cause and the operator should see them together.
    """
    failures: list[str] = []
    variables = dict(case.variables)
    if "question" in spec.required_variables() and "question" not in variables:
        variables["question"] = case.id

    rendered = render(spec, variables, apply_guardrails=False)

    started = time.perf_counter()
    if case.answer:
        # Golden-answer case: a pinned model reply used to self-test the harness
        # (schema + assertions) with no model in the loop. Contract evidence, not
        # model-quality evidence — the report marks these as ``golden=True``.
        output = case.answer
        golden = True
    else:
        golden = False
        try:
            output = llm(rendered.messages, rendered.parameters)
        except Exception as exc:  # a raising stub/gateway is a failed case, not a crash
            output = ""
            failures.append(f"llm_error:{type(exc).__name__}:{exc}")
    latency_ms = int((time.perf_counter() - started) * 1000)
    output = output or ""
    output_tokens = estimate_tokens(output)

    if case.max_latency_ms and latency_ms > case.max_latency_ms:
        failures.append(f"latency_ms:{latency_ms}>{case.max_latency_ms}")
    if case.max_output_tokens and output_tokens > case.max_output_tokens:
        failures.append(f"output_tokens:{output_tokens}>{case.max_output_tokens}")

    guard = check_output(output, spec.guardrails)
    expected_violations = set(case.expect_guardrail_violations)
    detected = set(guard.violations)
    for violation in sorted(expected_violations - detected):
        failures.append(f"guardrail_not_detected:{violation}")
    for violation in sorted(detected - expected_violations):
        failures.append(f"guardrail:{violation}")

    schema_errors: list[str] = []
    payload: Any = None
    # Assertions run against the guardrail-sanitized text: that is what the user
    # actually receives. A model that emits PII gets flagged through
    # ``expect_guardrail_violations``, and the sanitized text is then checked to
    # prove the value no longer leaks.
    checked = guard.text
    if spec.output_schema and not case.skip_schema_check:
        payload = extract_json(checked)
        if payload is None:
            schema_errors = ["$: output is not valid JSON"]
        else:
            schema_errors = validate(payload, spec.output_schema)
        if case.expect_schema_valid and schema_errors:
            failures.append("schema:" + describe_errors(schema_errors))
        if not case.expect_schema_valid and not schema_errors:
            failures.append("schema:expected_invalid_but_validated")

    for needle in case.must_contain:
        if needle not in checked:
            failures.append(f"missing:{needle!r}")
    for needle in case.must_not_contain:
        if needle in checked:
            failures.append(f"forbidden:{needle!r}")

    for path, expected in case.expect_paths.items():
        found, actual = _get_path(payload, path)
        if not found:
            failures.append(f"path_missing:{path}")
        elif actual != expected:
            failures.append(f"path_value:{path}={actual!r}!={expected!r}")

    return EvalCaseResult(
        case_id=case.id,
        passed=not failures,
        failures=failures,
        latency_ms=latency_ms,
        output_tokens=output_tokens,
        output=output,
        sanitized_output=checked,
        schema_errors=schema_errors,
        golden=golden,
    )


def run_prompt_evals(
    spec: PromptSpec,
    llm: LLMCallable,
    *,
    case_ids: list[str] | None = None,
    golden_only: bool = False,
    library_snapshot: dict[str, str] | None = None,
) -> PromptEvalReport:
    """Run every (or the selected) eval case for ``spec`` against ``llm``.

    ``golden_only=True`` runs only cases that pin a ``answer`` — the suite that
    is meaningful **without a model**. CI uses it on hosts without a gateway;
    a host with a gateway runs the full suite. The flag is explicit rather than
    auto-detected so a report always says which suite produced it.
    """
    wanted = set(case_ids) if case_ids else None
    selected = [
        c
        for c in spec.eval_cases
        if (wanted is None or c.id in wanted) and (not golden_only or bool(c.answer))
    ]
    started_at = datetime.now(UTC).isoformat()
    t0 = time.perf_counter()
    results = [_check_case(spec, case, llm) for case in selected]
    duration_ms = int((time.perf_counter() - t0) * 1000)
    return PromptEvalReport(
        ref=spec.ref,
        content_hash=spec.content_hash(),
        results=results,
        model=spec.parameters.model,
        started_at=started_at,
        duration_ms=duration_ms,
        library_snapshot=dict(library_snapshot or {}),
    )


