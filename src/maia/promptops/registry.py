"""Prompt registry: version resolution, diffing and change detection.

This is the "library" half of PromptOps. It answers the three questions a
reviewer or an incident responder actually asks:

* *Which prompts exist and what is the current release?*
  ``list_prompts`` / ``resolve("campaign_copywriter")``
* *What changed between two versions?* ``diff(a, b)`` — a structured,
  field-level diff (system prompt, user template, parameters, schema, few-shot)
  so a prompt PR can be reviewed like a code PR.
* *Did someone edit a prompt file while the service was running?*
  ``reload_if_changed()`` — the registry fingerprints mtimes+sizes, so a rolling
  deploy picks up new prompt text without a restart and an eval report can be
  tied to the exact content hash.

Failure policy: a broken prompt file is **excluded**, never silently defaulted.
``errors`` surfaces the reason so CI can fail on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .loader import PromptLoadError, PromptLoadResult, load_dir
from .models import PromptSpec

__all__ = ["PromptDiff", "PromptRegistry", "default_registry"]


@dataclass(frozen=True)
class PromptDiff:
    """Field-level difference between two prompt versions."""

    left: str
    right: str
    identical: bool
    changed_fields: dict[str, tuple[object, object]]

    def summary(self) -> str:
        if self.identical:
            return f"{self.left} == {self.right} (no content change)"
        fields = ", ".join(sorted(self.changed_fields))
        return f"{self.left} -> {self.right}: changed {fields}"


class PromptRegistry:
    """In-memory index over a prompt directory, with reload-on-change support."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._result: PromptLoadResult = PromptLoadResult(specs={}, errors=[])
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()
        self.reload()

    # ---- loading ---------------------------------------------------------
    def _compute_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        if not self.root.exists():
            return ()
        entries = []
        for p in sorted(self.root.rglob("*.y*ml")):
            try:
                st = p.stat()
            except OSError:  # pragma: no cover - race with an external delete
                continue
            entries.append((str(p.relative_to(self.root)), st.st_mtime_ns, st.st_size))
        return tuple(entries)

    def reload(self) -> PromptLoadResult:
        self._result = load_dir(self.root)
        self._fingerprint = self._compute_fingerprint()
        return self._result

    def reload_if_changed(self) -> bool:
        """Reload when files changed on disk; True when a reload happened."""
        if self._compute_fingerprint() != self._fingerprint:
            self.reload()
            return True
        return False

    # ---- introspection ---------------------------------------------------
    @property
    def errors(self) -> list[PromptLoadError]:
        return list(self._result.errors)

    def all(self) -> list[PromptSpec]:
        """Every loaded version, grouped by name then ascending semver."""
        return sorted(self._result.specs.values(), key=lambda s: (s.name, s.semver.key))

    def names(self) -> list[str]:
        return sorted({s.name for s in self._result.specs.values()})

    def versions(self, name: str) -> list[PromptSpec]:
        """All versions of ``name``, oldest → newest."""
        items = [s for s in self._result.specs.values() if s.name == name]
        return sorted(items, key=lambda s: s.semver.key)

    def latest(self, name: str) -> PromptSpec | None:
        """Highest semver regardless of ``status`` (used by diff/reporting)."""
        items = self.versions(name)
        return items[-1] if items else None

    def current(self, name: str) -> PromptSpec | None:
        """The version a *production* caller should get.

        Prefers the newest ``status == "active"`` release and only falls back to
        a draft when no active version exists — so a draft under review can never
        be picked up by the runtime by accident.
        """
        items = [s for s in self.versions(name) if s.status == "active"]
        return items[-1] if items else self.latest(name)

    def resolve(self, name: str, constraint: str | None = None) -> PromptSpec | None:
        """Resolve ``name`` with an optional semver constraint.

        ``resolve("nl_to_sql")`` → newest active; ``resolve("nl_to_sql", "1.0.0")``
        → exactly that version; ``resolve("nl_to_sql", "^1.0.0")`` → newest active
        inside major 1. Returns ``None`` when nothing matches.
        """
        if constraint in (None, "", "latest", "*"):
            return self.current(name)
        candidates = [s for s in self.versions(name) if s.semver.compatible_with(constraint)]
        if not candidates:
            return None
        active = [s for s in candidates if s.status == "active"]
        return (active or candidates)[-1]

    def get(self, ref: str) -> PromptSpec | None:
        """Look up an exact ``name@version`` reference."""
        return self._result.specs.get(ref)

    def require(self, name: str, constraint: str | None = None) -> PromptSpec:
        """Like :meth:`resolve` but raises — for code paths that cannot degrade."""
        spec = self.resolve(name, constraint)
        if spec is None:
            available = ", ".join(s.ref for s in self.versions(name)) or "none"
            raise KeyError(
                f"prompt {name!r} (constraint={constraint!r}) not found; "
                f"available versions: {available}"
            )
        return spec

    # ---- review support --------------------------------------------------
    def diff(self, left: PromptSpec | str, right: PromptSpec | str) -> PromptDiff:
        """Field-level diff between two versions (for prompt PR review)."""
        a = self.get(left) if isinstance(left, str) else left
        b = self.get(right) if isinstance(right, str) else right
        if a is None or b is None:
            missing = left if a is None else right
            raise KeyError(f"prompt version not found: {missing!r}")

        changed: dict[str, tuple[object, object]] = {}
        if a.system_prompt != b.system_prompt:
            changed["system_prompt"] = (a.system_prompt, b.system_prompt)
        if a.user_template != b.user_template:
            changed["user_template"] = (a.user_template, b.user_template)
        if a.parameters.model_dump() != b.parameters.model_dump():
            changed["parameters"] = (a.parameters.model_dump(), b.parameters.model_dump())
        if a.output_schema != b.output_schema:
            changed["output_schema"] = (a.output_schema, b.output_schema)
        if [e.model_dump() for e in a.few_shot] != [e.model_dump() for e in b.few_shot]:
            changed["few_shot"] = (
                [e.model_dump() for e in a.few_shot],
                [e.model_dump() for e in b.few_shot],
            )
        if len(a.eval_cases) != len(b.eval_cases):
            changed["eval_cases"] = (len(a.eval_cases), len(b.eval_cases))
        if a.status != b.status:
            changed["status"] = (a.status, b.status)
        if a.content_hash() != b.content_hash() and not changed:
            # Defensive: any future content field not enumerated above still shows
            # up as a change instead of a false "identical".
            changed["content_hash"] = (a.content_hash(), b.content_hash())
        return PromptDiff(
            left=a.ref, right=b.ref, identical=not changed, changed_fields=changed
        )

    def snapshot(self) -> dict[str, str]:
        """``ref -> content_hash`` map, for eval-report provenance."""
        return {
            ref: spec.content_hash() for ref, spec in sorted(self._result.specs.items())
        }


_DEFAULT: PromptRegistry | None = None


def default_registry(root: str | Path | None = None) -> PromptRegistry:
    """Process-wide registry rooted at ``settings.PROMPTS_DIR``.

    Lazily created and cached on first use, mirroring the repo's other
    settings-driven singletons; pass ``root`` to get an isolated registry.
    """
    if root is not None:
        return PromptRegistry(root)
    global _DEFAULT
    if _DEFAULT is None:
        from ..config import settings

        _DEFAULT = PromptRegistry(settings.PROMPTS_DIR)
    return _DEFAULT

