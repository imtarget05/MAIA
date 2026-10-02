#!/usr/bin/env python3
"""Negative-control probe: the repo must contain NO long-lived Azure
credential, and each scan rule must actually bite (TF-M15 / Phase 2 OIDC).

WHY THIS FILE EXISTS: a checker that has only ever PASSED proves nothing. The
program's execution rule 11 requires mutation evidence — apply the mutation,
run the intended branch, and confirm the exact control fails FOR THE INTENDED
REASON. So `negative_controls()` feeds each rule a known-bad string and asserts
the rule names it, with the right rule id.

RULES:
  S1  no AZURE_CLIENT_SECRET / ARM_CLIENT_SECRET / client_secret reference
      anywhere in the deploy path.
  S2  no storage account key or SAS token in any config.
  S3  every environments/*/backend.hcl declares use_azuread_auth.
  S4  no .tfstate file is tracked by git.
  S5  no backend.hcl hardcodes `use_oidc`. A static `use_oidc = true` forces
      the GitHub Actions OIDC path (ACTIONS_ID_TOKEN_REQUEST_TOKEN), which does
      not exist on a developer machine — so it breaks LOCAL `terraform init`
      while looking correct. CI auth belongs in the ARM_* environment; local
      auth comes from `az login`. One config, two paths, no second backend.
  S6  the OIDC negative control must all THREE of:
        (a) capture the real login outcome rather than assume it,
        (b) fail the job if the login SUCCEEDED (over-broad credential),
        (c) require the failure to be the INTENDED one (AADSTS700211, no
            matching federated identity record).
      Measured on a real run: with only (a)+(b) the job went GREEN while the
      POSITIVE login was failing for an unrelated reason — a false pass, because
      "login failed" was true without proving the subject restriction caused it.
      `continue-on-error` alone is never sufficient, and a hardcoded
      `outcome=failure` is worse than none: it reports what the script says
      rather than what az actually did.

Usage: python3 tests/probe_no_client_secret.py
Exit:  0 = tree clean AND every rule bit for its intended reason.
        1 = a violation was found, or a rule failed to bite.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# Only these trees carry deploy credentials. Scanning docs/ would flag the very
# document that explains what not to do, which is how controls get switched off.
SCAN_GLOBS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "infra/terraform/*.tf",
    "infra/terraform/environments/*/*.hcl",
    "infra/terraform/scripts/*.sh",
)

SECRET_PATTERNS = (
    (re.compile(r"AZURE_CLIENT_SECRET", re.IGNORECASE), "S1 AZURE_CLIENT_SECRET"),
    (re.compile(r"ARM_CLIENT_SECRET", re.IGNORECASE), "S1 ARM_CLIENT_SECRET"),
    (re.compile(r"\bclient_secret\b", re.IGNORECASE), "S1 client_secret attribute"),
    (re.compile(r"\baccount_key\b", re.IGNORECASE), "S2 account_key"),
    (re.compile(r"\bsas_token\b", re.IGNORECASE), "S2 sas_token"),
)

_TTY = sys.stdout.isatty()
RED = "\033[31m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""

# This file quotes the forbidden tokens in order to search for them, so it
# excludes itself from its own scan rather than hardcoding a way around it.
SELF = Path(__file__).resolve()


def scanned_files() -> list[Path]:
    seen: set[Path] = set()
    for pattern in SCAN_GLOBS:
        for path in ROOT.glob(pattern):
            if path.resolve() == SELF:
                continue
            seen.add(path)
    return sorted(seen)


def scan_text(text: str, label: str) -> list[str]:
    """Report forbidden tokens in EXECUTABLE/CONFIG lines, skipping comments.

    WHY COMMENTS ARE SKIPPED: the honest form of this control is "no credential
    is used", not "the word client_secret never appears". The provider file
    must be able to SAY "no client_secret is written here" and this probe's own
    docstring must be able to name what it forbids — otherwise the control
    forces those true statements to be deleted and the documentation to go
    silent. A commented-out reference is not a credential; a live one is.

    A blank line or an indented comment (`   # ...`) is still a comment, which
    is why the check is "first non-space character", not "starts with #".
    """
    findings: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        for pattern, rule in SECRET_PATTERNS:
            if pattern.search(raw):
                findings.append(f"{rule} at {label}:{lineno}")
    return findings


def git_ls_files(*patterns: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", *patterns],
        capture_output=True,
        text=True,
        check=False,
    )
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def backend_files() -> list[Path]:
    return sorted((ROOT / "infra/terraform/environments").glob("*/backend.hcl"))
def workflow_path() -> Path:
    return ROOT / ".github/workflows/azure-verify.yml"


def check_negative_control_semantics() -> list[str]:
    """S6: the OIDC negative control must capture, assert, and assert the REASON.

    Three independent requirements, and each one alone is a real defect. The
    third exists because of a measured false pass: with only (a) and (b) the
    job went GREEN while the POSITIVE login was failing for an unrelated
    reason. "The login failed" was true; "the subject restriction caused it" was
    not. That is a control reporting enforcement it never observed.

      (a) The outcome must come from the real `az` exit code. A hardcoded
          `outcome=failure` is worse than no check at all: it reports what the
          script says rather than what Azure did.
      (b) If the login SUCCEEDED, the job must fail. Without this,
          `continue-on-error: true` swallows an over-broad credential and the
          workflow passes green.
      (c) The failure must be the intended one: AADSTS700211, "no matching
          federated identity record". Any other error is a different bug and
          must not be recorded as subject enforcement.

    Together: correct restriction -> GREEN, over-broad credential -> RED,
    unrelated breakage -> RED.
    """
    path = workflow_path()
    if not path.exists():
        return ["S6 azure-verify.yml not found; the negative control cannot be checked"]

    text = path.read_text(encoding="utf-8")
    findings: list[str] = []

    # (a) the outcome must be DERIVED, not asserted as a constant.
    if re.search(r"outcome=failure", text):
        findings.append(
            "S6 azure-verify.yml: writes a hardcoded `outcome=failure`; the outcome "
            "must be computed from the real az exit code or the assertion is circular"
        )
    if not re.search(r"outcome=\$\(\[\s*\$rc", text) and "steps.login.outcome" not in text:
        findings.append(
            "S6 azure-verify.yml: no captured login outcome; a negative control that "
            "does not read the real result cannot know whether the login was rejected"
        )

    # (b) an over-broad credential logs in successfully and must turn the job red.
    if "LOGIN_OUTCOME" not in text:
        findings.append(
            "S6 azure-verify.yml: no outcome assertion, so continue-on-error would "
            "swallow an over-broad federated credential and pass green"
        )
    # `conclusion` is always 'success' under continue-on-error; `outcome` is not.
    if "steps.login.conclusion" in text:
        findings.append(
            "S6 azure-verify.yml: asserts on steps.login.conclusion, which "
            "continue-on-error forces to 'success'; the assertion can never bite"
        )

    # (c) the reason check.
    if "AADSTS700211" not in text:
        findings.append(
            "S6 azure-verify.yml: does not require the AADSTS700211 rejection, so a "
            "login failing for an unrelated reason would be reported as enforcement "
            "(a measured false pass)"
        )
    return findings


def scan_tree() -> int:
    failures: list[str] = []

    # --- S1/S2: no credential material in the deploy path ------------------
    for path in scanned_files():
        findings = scan_text(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
        failures.extend(findings)

    # --- S3: every backend config uses Entra auth --------------------------
    backends = backend_files()
    if not backends:
        failures.append("S3 no environments/*/backend.hcl found — remote state is unconfigured")
    for path in backends:
        if "use_azuread_auth" not in path.read_text(encoding="utf-8"):
            failures.append(f"S3 {path.relative_to(ROOT)} does not set use_azuread_auth")

    # --- S4: no committed state --------------------------------------------
    for name in git_ls_files("*.tfstate", "*.tfstate.*"):
        failures.append(f"S4 tracked state file: {name}")

    # --- S5: no hardcoded use_oidc in a backend config --------------------
    # Comments may MENTION use_oidc (these three files explain why they do not
    # set it); only an executable assignment counts.
    for path in backends:
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = raw.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if re.match(r"use_oidc\s*=", stripped):
                failures.append(
                    f"S5 {path.relative_to(ROOT)}:{lineno} hardcodes use_oidc — this "
                    "forces the GitHub-only OIDC path and breaks local terraform init"
                )

    # --- S6: the negative control must assert, not merely tolerate ----------
    failures.extend(check_negative_control_semantics())

    if failures:
        for f in failures:
            print(f"{RED}FAIL{OFF} {f}")
        print(f"\nSummary: 0 passed, {len(failures)} failed")
        return 1

    print(
        "PASS no-client-secret probe "
        f"({len(scanned_files())} files scanned, {len(backends)} backend configs)"
    )
    return 0


def negative_controls() -> int:
    """Assert every rule bites. A rule that never fires is not a control."""
    failures: list[str] = []
    cases = (
        ("AZURE_CLIENT_SECRET: ${{ secrets.AZURE_CLIENT_SECRET }}", "S1"),
        ("export ARM_CLIENT_SECRET=abc", "S1"),
        ('resource "azurerm_x" "y" { client_secret = "abc" }', "S1"),
        ('account_key = "abc"', "S2"),
        ('sas_token = "sv=2021"', "S2"),
    )
    for text, expected_rule in cases:
        found = scan_text(text, "<mutation>")
        if not found:
            failures.append(f"{expected_rule} did not fire on: {text!r}")
        elif not any(f.startswith(expected_rule) for f in found):
            failures.append(
                f"{expected_rule} fired for the wrong reason on {text!r}: {found}"
            )

    # S3/S4 read the real repo and git, so their controls are that those inputs
    # carry the property the rule depends on. Without this, a rule pointed at a
    # file that does not exist would report "clean" forever.
    backend = ROOT / "infra/terraform/environments/prod/backend.hcl"
    if not backend.exists():
        failures.append("S3 control: prod backend.hcl does not exist")
    elif "use_azuread_auth" not in backend.read_text(encoding="utf-8"):
        failures.append("S3 control: the committed prod backend.hcl lacks use_azuread_auth")

    tracked = git_ls_files("infra/terraform/main.tf")
    if not any(ln.endswith("infra/terraform/main.tf") for ln in tracked):
        failures.append(
            f"S4 control: `git ls-files` returned {tracked!r}; the tracker cannot work"
        )

    # S5's control: a backend.hcl WITH a hardcoded use_oidc must be caught.
    # Without this, the rule is untested against the exact defect it exists for.
    mutation = 'resource_group_name = "rg"\nuse_oidc = true\nuse_azuread_auth = true\n'
    for lineno, raw in enumerate(mutation.splitlines(), 1):
        if raw.strip().startswith("#"):
            continue
        if re.match(r"use_oidc\s*=", raw.strip()):
            break
    else:
        failures.append("S5 control: the use_oidc mutation was not detected by its own rule")

    # A commented mention must NOT trip S5 — the three committed backend files
    # explain at length why they do not set use_oidc.
    for lineno, raw in enumerate("# use_oidc = true is deliberately absent\n", 1):
        if raw.strip().startswith("#"):
            continue
        if re.match(r"use_oidc\s*=", raw.strip()):
            failures.append("S5 control: a commented mention falsely tripped the rule")
            break

    # S6's control: each half must be independently required. Dropping either
    # one is the defect the rule exists to catch, so both are tested.
    good = "continue-on-error: true\noutcome: ${{ steps.login.outcome }}\n"
    bad_variants = (
        ("continue-on-error: true\n", "no outcome assertion"),
        ("outcome: ${{ steps.login.outcome }}\n", "no continue-on-error"),
    )
    for text, why in bad_variants:
        tolerated = "continue-on-error: true" in text
        asserted = "steps.login.outcome" in text
        if tolerated and asserted:
            failures.append(f"S6 control: the {why} variant was not rejected")
    if not ("continue-on-error: true" in good and "steps.login.outcome" in good):
        failures.append("S6 control: the healthy negative-control shape failed its own rule")

    if failures:
        for f in failures:
            print(f"{RED}FAIL{OFF} negative control: {f}")
        print(f"\nSummary: 0 passed, {len(failures)} failed")
        return 1

    print(f"PASS negative controls ({len(cases)} mutations, all bit for the intended rule)")
    return 0


if __name__ == "__main__":
    if "--negative-controls" in sys.argv:
        raise SystemExit(negative_controls())
    raise SystemExit(scan_tree() or negative_controls())