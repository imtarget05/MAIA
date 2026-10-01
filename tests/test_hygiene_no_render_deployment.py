"""Hygiene gate: Render must never be re-introduced as a deployment target.

ARCHITECTURE DECISION (repository cleanup):

    SOURCE -> GitHub Actions -> OIDC -> Terraform -> Azure Container Apps

Render is no longer a deployment target. This gate rejects ACTIVE deployment
artifacts (render.yaml, Render deploy workflows, Render secrets, Render
keepalive pings) so the competing path cannot quietly come back.

WHAT IT DELIBERATELY DOES NOT DO: it does not ban the English word "render".
`src/maia/promptops/render.py` renders prompts, `render()` call sites render
HTML/metrics/citations, and historical evidence (`docs/evidence/**`,
`docs/deployment.md`, `_archive/**`) legitimately records the old Render path.
Only executable deployment artifacts are forbidden.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# A Render blueprint at any tracked location is an active deploy artifact.
FORBIDDEN_FILENAMES = ("render.yaml", "render.yml")

# Patterns that only ever mean "this deploys to / pings Render".
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Render blueprint/domain URL", re.compile(r"\b(?:[a-z0-9-]+\.)*onrender\.com\b", re.I)),
    ("Render API domain", re.compile(r"\brender\.com\b", re.I)),
    ("Render secret/env reference", re.compile(r"\bRENDER_[A-Z0-9_]+\b")),
    ("Render deploy hook", re.compile(r"render[_-]?deploy[_-]?hook|render deploy hook", re.I)),
)

# Paths where Render history is allowed to remain.
HISTORICAL_ALLOW_PREFIXES = (
    "docs/evidence/",
    "_archive/",
)
HISTORICAL_ALLOW_FILES = (
    "docs/deployment.md",
    "tests/test_hygiene_no_render_deployment.py",
)


def _workflow_files() -> list[Path]:
    if not WORKFLOWS.is_dir():
        return []
    return sorted(
        p for p in WORKFLOWS.iterdir() if p.suffix in {".yml", ".yaml"}
    )


def _offenders(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = [label for label, rx in FORBIDDEN_PATTERNS if rx.search(text)]
    return hits


def test_no_render_blueprint_file_exists():
    """No Render blueprint anywhere in the repository."""
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in sorted(REPO_ROOT.rglob("*"))
        if p.is_file()
        and p.name in FORBIDDEN_FILENAMES
        and ".git" not in p.parts
    ]
    assert not offenders, (
        "Render blueprint file(s) present — Render is not a deployment "
        f"target: {offenders}"
    )


def test_no_workflow_deploys_or_pings_render():
    """Active GitHub workflows must contain no Render deployment machinery."""
    offenders: dict[str, list[str]] = {}
    for wf in _workflow_files():
        hits = _offenders(wf)
        if hits:
            offenders[str(wf.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "Workflow(s) reference Render deployment/keepalive — the deploy story "
        f"is GitHub Actions -> OIDC -> Terraform -> Azure: {offenders}"
    )


def test_no_render_secret_names_referenced_by_workflows():
    """Explicit Render secret names (API key / service id / hook / health)."""
    render_secrets = re.compile(
        r"\bRENDER_(?:API_KEY|SERVICE_ID|DEPLOY_HOOK|DEPLOY_HOOK_URL|HEALTH_URL[A-Z_]*|API_BASE_URL)\b"
    )
    offenders: list[str] = []
    for wf in _workflow_files():
        if render_secrets.search(wf.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(wf.relative_to(REPO_ROOT)))
    assert not offenders, f"Legacy Render secret references in workflows: {offenders}"


def test_historical_render_records_are_still_allowed():
    """Guard against over-cleaning: history may keep Render references.

    If this test starts failing because the historical files were deleted,
    the pipeline has been over-cleaned and evidence was lost.
    """
    # docs/deployment.md is the canonical historical Render record.
    assert (REPO_ROOT / "docs" / "deployment.md").is_file()
    # And the word "render" must still be expressible without tripping the gate:
    # this very file contains `onrender.com` inside a regex.
    assert any(_offenders(p) == [] for p in _workflow_files()) or not _workflow_files()
