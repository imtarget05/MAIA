"""Render a :class:`~maia.promptops.models.PromptSpec` into chat messages.

Substitution is deliberately a plain ``str.replace`` over ``{name}`` tokens, not
``str.format`` / ``jinja2``:

* Values containing braces (JSON, code, reviews) cannot break rendering or be
  re-interpreted as new placeholders — the classic ``format`` injection where a
  user-supplied ``{0.__class__}`` reaches into the process.
* Only the variables the spec declares are substituted; anything else stays
  literal, so an unfilled placeholder is visible in the prompt rather than
  silently blank.

Missing variables raise. Extra variables are accepted but reported, because
callers legitimately pass a superset (one context dict for several prompts) and
failing on that would push defensive ``**kwargs`` filtering into every caller.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .guardrails import sanitize_input
from .models import PromptSpec

__all__ = ["RenderedPrompt", "render"]


@dataclass(frozen=True)
class RenderedPrompt:
    ref: str
    messages: list[dict[str, str]]
    parameters: dict[str, Any]
    content_hash: str
    used_variables: dict[str, str] = field(default_factory=dict)
    ignored_variables: list[str] = field(default_factory=list)
    guardrail_flags: list[str] = field(default_factory=list)

    def audit_record(self) -> dict[str, Any]:
        """Compact provenance record (no prompt text, no variable values).

        Safe to log/return: it proves *which* prompt version and *which* hash
        produced an answer without leaking tenant data into logs.
        """
        return {
            "prompt": self.ref,
            "content_hash": self.content_hash,
            "parameters": self.parameters,
            "variables": sorted(self.used_variables),
            "guardrail_flags": list(self.guardrail_flags),
        }


def _require_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _substitute(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def render(
    spec: PromptSpec,
    variables: dict[str, Any] | None = None,
    *,
    sanitize_external: Iterable[str] = (),
    apply_guardrails: bool = True,
) -> RenderedPrompt:
    """Build the ``messages`` list for ``spec``.

    ``sanitize_external`` names variables that carry untrusted text (scraped
    reviews, user questions). They are neutralised against prompt injection
    before interpolation, and the applied rules produce ``guardrail_flags``
    entries so the caller can log that a prompt consumed dirty input.
    """
    values: dict[str, str] = {}
    flags: list[str] = []
    raw = dict(variables or {})
    external = set(sanitize_external)

    for key, value in raw.items():
        text = _require_text(value)
        if apply_guardrails and key in external:
            outcome = sanitize_input(text)
            text = outcome.text
            flags.extend(f"{key}:{v}" for v in outcome.violations)
        values[key] = text

    required = spec.required_variables()
    missing = sorted(v for v in required if v not in values)
    if missing:
        raise ValueError(
            f"{spec.ref}: missing required prompt variables {missing}; "
            f"provided {sorted(values)}"
        )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _substitute(spec.system_prompt, values)}
    ]

    for example in spec.few_shot:
        example_values = {k: _require_text(v) for k, v in example.input.items()}
        demo = _substitute(spec.user_template, example_values)
        if example.output:
            messages.append({"role": "user", "content": demo})
            messages.append({"role": "assistant", "content": example.output})

    messages.append({"role": "user", "content": _substitute(spec.user_template, values)})

    parameters = {
        k: v for k, v in spec.parameters.model_dump().items() if k != "rationale"
    }
    return RenderedPrompt(
        ref=spec.ref,
        messages=messages,
        parameters=parameters,
        content_hash=spec.content_hash(),
        used_variables={k: v for k, v in values.items() if k in required},
        ignored_variables=sorted(k for k in values if k not in required),
        guardrail_flags=flags,
    )
