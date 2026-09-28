"""PromptOps data models — the contract every prompt in ``prompts/`` must satisfy.

Design notes (why this is not just "a YAML file"):

* **Versioning is first-class.** ``SemVer`` + a filename convention
  (``<name>.v<major>.<minor>.<patch>.yaml``) means the prompt library is
  reviewable in Git the same way code is, and a prompt change is a diffable,
  revertable, release-able artefact — the JD's "prompt library under version
  control" requirement.
* **Parameters are bounded.** ``temperature`` 0–2, ``top_p`` 0–1,
  ``max_tokens`` > 0: a typo such as ``temperature: 20`` fails at load time
  instead of silently degrading every response in production.
* **Every prompt can declare an output contract.** ``output_schema`` is a
  JSON-Schema document (Draft 2020-12, via :mod:`maia.json_schema_lite`); the eval
  harness refuses to mark a case as passing if the model's answer does not validate.
* **Content hash.** ``content_hash`` pins the exact prompt text a run used, so
  an eval report from last month can be proven to belong to prompt v1.0.0 and
  not to an edited file with the same version string.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "EvalCase",
    "FewShotExample",
    "PromptParameter",
    "PromptSpec",
    "SemVer",
    "parse_version",
]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_FILENAME_RE = re.compile(
    r"^(?P<name>[a-z][a-z0-9_]*)\.v(?P<version>\d+\.\d+\.\d+)\.ya?ml$"
)


class SemVer:
    """Minimal, dependency-free semantic version with correct ordering.

    Only what prompt release management needs: parse, compare, and a
    ``compatible_with`` check for the ``^1.2.0`` style constraint used when a
    caller asks for "the current major line".
    """

    __slots__ = ("major", "minor", "patch")

    def __init__(self, major: int, minor: int, patch: int) -> None:
        self.major, self.minor, self.patch = major, minor, patch

    @classmethod
    def parse(cls, text: str) -> SemVer:
        m = _VERSION_RE.match((text or "").strip())
        if not m:
            raise ValueError(f"not a semantic version: {text!r} (expected MAJOR.MINOR.PATCH)")
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def compatible_with(self, constraint: str) -> bool:
        """``^1.2.0`` / ``~1.2.0`` / ``1.2.0`` / ``1.x`` — no external dep."""
        c = (constraint or "").strip()
        if c in ("", "*", "latest"):
            return True
        if c.startswith("^"):
            base = SemVer.parse(c[1:])
            return self.major == base.major and self.key >= base.key
        if c.startswith("~"):
            base = SemVer.parse(c[1:])
            return self.key[:2] == base.key[:2] and self.key >= base.key
        if c.lower().endswith(".x") and c.lower().count(".") == 1:
            # "1.x" matches any 1.*; "1.4.x" must also pin the minor, so the
            # generic x-branch below handles that case.
            return self.major == int(c.split(".")[0])
        if "x" in c.lower():
            parts = c.lower().split(".")
            if parts[0] != "x" and self.major != int(parts[0]):
                return False
            # Keep the minor check explicit: a reader auditing constraint matching
            # must see that "1.4.x" really pins the minor.
            if len(parts) > 1 and parts[1] != "x":
                return self.minor == int(parts[1])
            return True
        return self.key == SemVer.parse(c).key

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.major}.{self.minor}.{self.patch}"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SemVer('{self}')"

    def __eq__(self, other: object) -> bool:
        # Two lines rather than `isinstance(...) and ...`: the guard is a security-
        # adjacent contract (a foreign object must never compare equal), and the
        # explicit form is what a reader expects.
        if not isinstance(other, SemVer):
            return False
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __lt__(self, other: SemVer) -> bool:
        return self.key < other.key

    def __le__(self, other: SemVer) -> bool:
        return self.key <= other.key

    def __gt__(self, other: SemVer) -> bool:
        return self.key > other.key

    def __ge__(self, other: SemVer) -> bool:
        return self.key >= other.key


def parse_version(text: str) -> tuple[int, int, int]:
    """Convenience for sorting/filtering without constructing ``SemVer``."""
    return SemVer.parse(text).key


def filename_parts(filename: str) -> tuple[str, str]:
    """Extract ``(name, version)`` from ``campaign_copywriter.v1.0.0.yaml``."""
    m = _FILENAME_RE.match(filename)
    if not m:
        raise ValueError(
            f"prompt filename {filename!r} must match "
            "'<name>.v<major>.<minor>.<patch>.yaml'"
        )
    return m.group("name"), m.group("version")


class PromptParameter(BaseModel):
    """Model parameters declared *with* the prompt, not in the caller's code.

    The whole point of PromptOps is that tuning lives next to the prompt text
    under review: a change from ``temperature: 0.2`` to ``0.9`` shows up in the
    prompt's Git diff and in :func:`maia.promptops.registry.PromptRegistry.diff`.
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    max_tokens: int = Field(default=800, gt=0, le=32000)
    model: str = ""
    stop: list[str] = Field(default_factory=list)
    # Explicitly documents *why* these parameters were chosen — required by the
    # review checklist in prompts/README.md for any non-default value.
    rationale: str = ""


class FewShotExample(BaseModel):
    """One demonstration pair. ``input`` fills ``user_template`` variables."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    output: str = ""
    note: str = ""


class EvalCase(BaseModel):
    """A single regression test for a prompt (offline, deterministic).

    A case is *only* about contract compliance — schema validity, required and
    forbidden substrings, latency/token budgets, and exact JSON path values.
    Semantic quality is measured by the retrieval eval harness
    (:mod:`maia.eval`), not here; mixing the two makes both flaky.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    note: str = ""
    variables: dict[str, Any] = Field(default_factory=dict, alias="vars")
    expect_schema_valid: bool = True
    # ``skip_schema_check`` is for cases that deliberately exercise guardrails on
    # free-text output (e.g. "PII in the answer must be detected"), where the
    # prompt's JSON contract is not the subject under test.
    skip_schema_check: bool = False
    # Violations that are EXPECTED: the case passes when they fire. This is how a
    # guardrail gets a positive test instead of a permanently-red case.
    expect_guardrail_violations: list[str] = Field(default_factory=list)
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    max_latency_ms: int = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=0, ge=0)
    expect_paths: dict[str, Any] = Field(default_factory=dict)
    # ``answer`` lets a case pin the exact stub-LLM reply (golden output) so the
    # harness itself is tested without any model in the loop.
    answer: str = ""

    @field_validator("must_contain", "must_not_contain")
    @classmethod
    def _no_blank_needles(cls, value: list[str]) -> list[str]:
        if any(not str(v).strip() for v in value):
            raise ValueError("must_contain/must_not_contain entries cannot be blank")
        return value


class PromptSpec(BaseModel):
    """A versioned, reviewable prompt artefact loaded from ``prompts/**``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    title: str = ""
    description: str = ""
    category: str = "general"
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    status: str = "active"  # active | draft | deprecated
    supersedes: str = ""

    system_prompt: str
    user_template: str
    parameters: PromptParameter = Field(default_factory=PromptParameter)
    few_shot: list[FewShotExample] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    guardrails: list[str] = Field(default_factory=list)
    eval_cases: list[EvalCase] = Field(default_factory=list)
    changelog: str = ""

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                f"prompt name {value!r} must be snake_case, 3-64 chars, "
                "starting with a letter"
            )
        return value

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        SemVer.parse(value)  # raises ValueError with an actionable message
        return value

    @field_validator("system_prompt")
    @classmethod
    def _non_empty_system(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("system_prompt cannot be empty")
        return value

    @field_validator("category")
    @classmethod
    def _valid_category(cls, value: str) -> str:
        if value not in {"marketing", "product", "analytics", "operations", "general"}:
            raise ValueError(
                "category must be one of marketing|product|analytics|operations|general"
            )
        return value

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str) -> str:
        if value not in {"active", "draft", "deprecated"}:
            raise ValueError("status must be one of active|draft|deprecated")
        return value

    @model_validator(mode="after")
    def _check_guardrail_rules(self) -> PromptSpec:
        """Reject unknown guardrail rule names at load time.

        A typo like ``no_pii_`` would otherwise produce a prompt that *looks*
        guarded but enforces nothing — the failure mode is silent and only shows
        up as a privacy incident.
        """
        from .guardrails import validate_rule_names

        unknown = validate_rule_names(self.guardrails)
        if unknown:
            raise ValueError(
                f"unknown guardrail rule(s) {unknown}; "
                "supported rules live in maia.promptops.guardrails.GUARDRAIL_RULES"
            )
        return self

    @model_validator(mode="after")
    def _check_eval_case_ids_unique(self) -> PromptSpec:
        seen: set[str] = set()
        for case in self.eval_cases:
            if case.id in seen:
                raise ValueError(f"duplicate eval case id {case.id!r} in {self.name}@{self.version}")
            seen.add(case.id)
        return self

    @property
    def semver(self) -> SemVer:
        return SemVer.parse(self.version)

    @property
    def ref(self) -> str:
        """Canonical ``name@version`` handle used in logs, reports and API."""
        return f"{self.name}@{self.version}"

    def content_hash(self) -> str:
        """SHA-256 over the prompt *content* (not metadata like owner/tags).

        Two files with different owners but identical instructions produce the
        same hash — which is what an audit wants: "was the text the model saw
        the text we reviewed?".
        """
        payload = {
            "system_prompt": self.system_prompt,
            "user_template": self.user_template,
            "few_shot": [e.model_dump() for e in self.few_shot],
            "output_schema": self.output_schema,
            "parameters": {
                k: v
                for k, v in self.parameters.model_dump().items()
                if k != "rationale"
            },
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def required_variables(self) -> set[str]:
        """Template variables the caller MUST supply.

        Placeholders are the simple ``{name}`` form the repo already uses;
        literal braces are written ``{{`` / ``}}`` exactly like
        ``maia.langchain.prompts`` does for ``ChatPromptTemplate``.
        """
        pattern = re.compile(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})")
        found = set(pattern.findall(self.user_template))
        found |= set(pattern.findall(self.system_prompt))
        found.discard("few_shot")
        return found


