"""PromptOps — versioned prompt library, structured output and eval gate.

Public surface (everything a caller needs; internals stay importable but are not
part of the contract):

* :class:`PromptRegistry` / :func:`default_registry` — discover, resolve by
  semver, diff versions, detect on-disk changes.
* :class:`PromptSpec` — validated prompt artefact (parameters constrained,
  eval cases declared, output schema typed, content hash for provenance).
* :func:`render` — build chat messages with strict variable checking and
  injection-neutralised external variables.
* :func:`run_prompt_evals` — offline regression gate returning a report that
  pins the prompt content hash.
* :func:`check_output` — enforce the prompt's declared guardrails.
"""
from __future__ import annotations

from .evals import (
    EvalCaseResult,
    PromptEvalError,
    PromptEvalReport,
    estimate_tokens,
    extract_json,
    run_prompt_evals,
)
from .guardrails import GuardrailOutcome, check_output, sanitize_input
from .loader import PromptLoadError, PromptLoadResult, load_dir, load_file
from .models import (
    EvalCase,
    FewShotExample,
    PromptParameter,
    PromptSpec,
    SemVer,
    filename_parts,
    parse_version,
)
from .registry import PromptDiff, PromptRegistry, default_registry
from .render import RenderedPrompt, render

__all__ = [
    "EvalCase",
    "EvalCaseResult",
    "FewShotExample",
    "GuardrailOutcome",
    "PromptDiff",
    "PromptEvalError",
    "PromptEvalReport",
    "PromptLoadError",
    "PromptLoadResult",
    "PromptParameter",
    "PromptRegistry",
    "PromptSpec",
    "RenderedPrompt",
    "SemVer",
    "check_output",
    "default_registry",
    "estimate_tokens",
    "extract_json",
    "filename_parts",
    "load_dir",
    "load_file",
    "parse_version",
    "render",
    "run_prompt_evals",
    "sanitize_input",
]
