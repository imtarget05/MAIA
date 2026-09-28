"""PromptOps guardrails: named, declarative checks attached to a prompt spec.

A prompt declares ``guardrails: [no_secret_leak, no_pii, max_chars:1200]`` and the
runner enforces exactly those. Two properties make this production-safe:

* **Reuse, not a second implementation.** The heavy lifting is delegated to the
  code that already protects the answer path — ``loops/guardrails.py``
  (``OutputGuardrail`` secret/PII/approval-claim scan, ``InputGuardrail``,
  ``DocumentSanitizer``) and ``pii.PIIScanner``. A prompt-level copy of the
  patterns would drift from the runtime guardrail, which is how leaks happen.
* **Fail-closed on the unknown.** An unrecognised rule name is a spec error: the
  prompt is rejected at load time instead of being silently "guarded" by an
  unimplemented rule.

Input sanitization is separated from output checking on purpose: variables that
carry *external* text (user input, scraped reviews) go through
:func:`sanitize_input` first, model answers go through :func:`check_output`.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..loops.guardrails import (
    DocumentSanitizer,
    InputGuardrail,
    OutputGuardrail,
    detect_injection,
)

__all__ = [
    "GUARDRAIL_RULES",
    "GuardrailOutcome",
    "check_output",
    "run_guardrails",
    "sanitize_input",
    "validate_rule_names",
]

# Vocabulary of supported rules. Parameterised forms use ``name:value``.
GUARDRAIL_RULES: frozenset[str] = frozenset(
    {
        "no_secret_leak",      # prompt/system-prompt disclosure or secret-looking text
        "no_pii",              # emails, phone numbers, national IDs -> redacted
        "no_approval_claim",   # must not claim an action ran without approval
        "no_injection",        # must not echo/obey injected instructions
        "max_chars",           # max_chars:<int> — hard cap, truncates
    }
)

_SANITIZE_KEEPS_DEFAULT = 2000


@dataclass(frozen=True)
class GuardrailOutcome:
    text: str
    violations: list[str]
    redacted: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations


def validate_rule_names(rules: list[str]) -> list[str]:
    """Return the rule names that are not in :data:`GUARDRAIL_RULES`."""
    unknown = []
    for rule in rules or []:
        name = str(rule).split(":", 1)[0].strip()
        if name not in GUARDRAIL_RULES:
            unknown.append(str(rule))
    return unknown


def _param(rule: str) -> tuple[str, str]:
    name, _, value = str(rule).partition(":")
    return name.strip(), value.strip()


def sanitize_input(text: str, *, max_length: int = _SANITIZE_KEEPS_DEFAULT) -> GuardrailOutcome:
    """Neutralise injection patterns in externally-supplied text.

    Used for *variables* (a scraped review, a user question) before they are
    interpolated into a prompt, so a prompt cannot be hijacked by its own data.
    """
    clean, flags = InputGuardrail(max_length=max_length).check(text or "")
    neutralized, dirty = DocumentSanitizer().sanitize(clean)
    violations = list(flags)
    if dirty and "injection_detected_in_query" not in violations:
        violations.append("injection_detected_in_query")
    return GuardrailOutcome(
        text=neutralized, violations=violations, redacted=dirty
    )


def check_output(text: str, rules: list[str] | None = None) -> GuardrailOutcome:
    """Apply the prompt's declared rules to a model answer.

    Returns the (possibly redacted/truncated) text plus the violation names. A
    violation does **not** raise: the caller decides whether to retry, refuse, or
    report — the eval harness treats any violation as a failed case, the API
    surfaces it in the response envelope.
    """
    rules = rules or []
    out = text or ""
    violations: list[str] = []
    redacted = False

    needs_secret = "no_secret_leak" in rules or "no_approval_claim" in rules
    if needs_secret:
        # The validity flag is intentionally dropped: violations are collected and
        # the caller decides whether to retry, degrade or refuse.
        _ok, issues, out = OutputGuardrail(redact_pii=False).check(out)
        violations.extend(issues)

    if "no_pii" in rules:
        from ..loops.pii import PIIScanner

        masked = PIIScanner.redact(out)
        if masked != out:
            out = masked
            redacted = True
            violations.append("pii_redacted_in_output")

    if "no_injection" in rules:
        hits = detect_injection(out)
        if hits:
            violations.append("injection_echoed_in_output")

    for rule in rules:
        name, value = _param(rule)
        if name != "max_chars":
            continue
        try:
            limit = int(value or 0)
        except ValueError:
            violations.append(f"invalid_max_chars_parameter:{rule}")
            continue
        if limit > 0 and len(out) > limit:
            out = out[:limit]
            violations.append("output_truncated_max_chars")

    return GuardrailOutcome(text=out, violations=violations, redacted=redacted)


def run_guardrails(text: str, rules: list[str] | None = None) -> GuardrailOutcome:
    """Alias kept for readability at call sites that check model output."""
    return check_output(text, rules)
