"""Loader for the on-disk prompt library (``prompts/**/*.yaml``).

Two invariants are enforced here, at the boundary, so nothing downstream has to
defend itself:

1. **Filename ↔ content agreement.** ``campaign_copywriter.v1.0.0.yaml`` must
   declare ``name: campaign_copywriter`` and ``version: 1.0.0``. Without this
   check a copy-paste ("duplicate v1.1.0 file, forgot to bump the field") would
   publish two different contents under one version — the exact failure mode
   version control is supposed to prevent.
2. **Documents must be mappings.** A YAML list or scalar at the top level is a
   file-format mistake, not a prompt; failing loudly beats returning a spec with
   empty defaults.

Load errors are collected (never raised per-file) by :func:`load_dir` so the
registry can report *all* broken prompts in one pass — an operator fixing a
library should not have to re-run the linter once per file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from .models import PromptSpec, filename_parts

__all__ = ["PromptLoadError", "PromptLoadResult", "load_dir", "load_file"]


@dataclass(frozen=True)
class PromptLoadError:
    path: str
    error: str


@dataclass(frozen=True)
class PromptLoadResult:
    specs: dict[str, PromptSpec]
    errors: list[PromptLoadError]

    @property
    def ok(self) -> bool:
        return not self.errors


def load_file(path: str | Path) -> PromptSpec:
    """Parse and validate a single prompt file.

    Raises ``ValueError`` (never a bare YAML/pydantic traceback) so callers get a
    message that names the file and the offending field.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{p.name}: cannot read prompt file ({exc})") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"{p.name}: invalid YAML ({exc})") from exc

    if not isinstance(data, dict):
        # ValueError, not TypeError (TRY004): the *prompt file* has the wrong shape,
        # and PromptRegistry surfaces loader problems as messages.
        raise ValueError(  # noqa: TRY004
            f"{p.name}: prompt file must be a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )

    try:
        name_from_file, version_from_file = filename_parts(p.name)
    except ValueError as exc:
        raise ValueError(f"{p.name}: {exc}") from exc

    declared_name = data.get("name")
    declared_version = data.get("version")
    if declared_name != name_from_file:
        raise ValueError(
            f"{p.name}: filename says name={name_from_file!r} but the document "
            f"declares name={declared_name!r}"
        )
    if str(declared_version) != version_from_file:
        raise ValueError(
            f"{p.name}: filename says version={version_from_file!r} but the "
            f"document declares version={declared_version!r}"
        )

    try:
        return PromptSpec(**data)
    except PydanticValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ValueError(f"{p.name}: invalid prompt spec ({details})") from exc


def load_dir(root: str | Path, *, skip_dirs: tuple[str, ...] = ("archive", "deprecated")) -> PromptLoadResult:
    """Load every ``*.yaml`` under ``root``, collecting errors instead of raising.

    ``skip_dirs`` keeps intentionally-retired prompts on disk (history matters)
    without publishing them as available versions.
    """
    base = Path(root)
    specs: dict[str, PromptSpec] = {}
    errors: list[PromptLoadError] = []
    if not base.exists():
        return PromptLoadResult(specs=specs, errors=errors)

    for path in sorted(base.rglob("*.y*ml")):
        rel_parts = path.relative_to(base).parts
        if any(part in skip_dirs for part in rel_parts[:-1]):
            continue
        try:
            spec = load_file(path)
        except ValueError as exc:
            errors.append(PromptLoadError(path=str(path), error=str(exc)))
            continue
        if spec.ref in specs:
            errors.append(
                PromptLoadError(
                    path=str(path),
                    error=(
                        f"duplicate prompt reference {spec.ref} "
                        f"(already loaded from another file)"
                    ),
                )
            )
            continue
        specs[spec.ref] = spec
    return PromptLoadResult(specs=specs, errors=errors)
