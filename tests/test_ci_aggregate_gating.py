"""The CI aggregate must gate on every non-advisory job.

Root cause: `.github/workflows/ci.yml` defines 14 jobs but the `notify`
aggregate job listed only 8 in `needs`. `gitleaks`, `bicep-validate`,
`coverage` and `promptops-mcp` were absent, so a red secret scan or a red IaC
validation could not flip the aggregate result the Discord notification and
any branch protection read.

Advisory jobs are the exception and stay non-blocking IN SPIRIT: `pip-audit`
and `sonarcloud-analysis` mark their single failure-relevant step
`continue-on-error: true`, so they can never fail the pipeline no matter what
the aggregate does. This test asserts the blocking set is gated AND that the
advisory set is still marked advisory -- the pair, because "gating everything"
would break the pipeline on unactionable findings.

Parsed from the workflow YAML, not regex-matched, so a renamed job cannot
silently drop out of the invariant.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CI_YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

#: Jobs whose failure-relevant step is explicitly `continue-on-error`, so a
#: failure is reported but never fails the run. Kept explicit here (rather than
#: derived) because the whole point is to pin WHICH findings are known
#: unactionable; a job that silently stops being advisory should fail CI.
ADVISORY_JOBS = {"pip-audit", "sonarcloud-analysis"}

#: The aggregate job itself is excluded from its own `needs`.
AGGREGATE_JOB = "notify"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(CI_YML.read_text())


def _advisory_steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if s.get("continue-on-error")]


def test_the_workflow_still_declares_every_job(workflow: dict):
    """A guard on the guard: if someone deletes a job, the subset check below
    would still pass on a smaller world.

    15 = the 14 original jobs + `abstention-gate-measurement`. Bump this
    number when a job is added or removed, and keep the count honest rather
    than loosening the assertion.
    """
    assert len(workflow["jobs"]) == 15


def test_every_non_advisory_job_is_in_the_aggregate_needs(workflow: dict):
    jobs = workflow["jobs"]
    needs = set(jobs[AGGREGATE_JOB]["needs"])

    blocking = {
        name
        for name, job in jobs.items()
        if name != AGGREGATE_JOB
        and not job.get("continue-on-error")
        and not _advisory_steps(job)
    }
    missing = blocking - needs
    assert not missing, (
        f"CI aggregate does not gate on {sorted(missing)}; "
        f"gated now: {sorted(needs)}"
    )


def test_the_aggregate_still_reports_every_job_it_needs(workflow: dict):
    """`needs` must not name a job that no longer exists: GitHub rejects the
    workflow outright in that case, which is a worse failure than the one this
    file exists to prevent."""
    jobs = workflow["jobs"]
    unknown = set(jobs[AGGREGATE_JOB]["needs"]) - set(jobs)
    assert not unknown


def test_the_aggregate_runs_even_when_a_dependency_fails(workflow: dict):
    """Without `always()` the notification would never fire on a red run --
    which is precisely the run an operator needs to be told about."""
    assert workflow["jobs"][AGGREGATE_JOB]["if"].strip() == "always()"


def test_advisory_jobs_are_still_marked_advisory(workflow: dict):
    """The pair to the gating test: `pip-audit` and `sonarcloud-analysis` must
    stay non-blocking, or an unactionable finding hard-fails every build."""
    jobs = workflow["jobs"]
    for name in ADVISORY_JOBS:
        job = jobs[name]
        assert _advisory_steps(job), (
            f"{name} is treated as advisory by this test but has no "
            f"continue-on-error step; make that disagreement explicit"
        )


def test_the_four_previously_ungated_jobs_are_now_gated(workflow: dict):
    """The named regression, spelled out so the diff that fixes it is obvious
    and so a future removal is a deliberate act."""
    needs = set(workflow["jobs"][AGGREGATE_JOB]["needs"])
    assert {"promptops-mcp", "coverage", "gitleaks", "bicep-validate"} <= needs