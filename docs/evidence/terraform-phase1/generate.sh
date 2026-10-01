#!/usr/bin/env bash
# Regenerates the Phase 1 gate evidence log next to this script.
#
# WHY this is a committed script and not a pasted terminal dump: a pasted log is
# a claim about what someone ran. This prints every number in the log from a
# command, so a reviewer can re-run it and compare, and a changed figure shows
# up as a changed file rather than as a stale sentence.
#
# Usage:  ./docs/evidence/terraform-phase1/generate.sh
# Writes: ./docs/evidence/terraform-phase1/<today>-gate.log
# Exit:   the gate's own exit code (0 = offline gate PASS)
#
# Offline by design: no Azure login, no plan against a subscription, no apply.
# For the authenticated variant use infra/terraform/scripts/phase1_check.sh
# --plan <env>, which is never run from here.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO" || exit 2

OUT="docs/evidence/terraform-phase1/$(date +%Y-%m-%d)-gate.log"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"

section() { printf '\n## %s\n' "$1" >>"$OUT"; }
p() { printf '%s\n' "$*" >>"$OUT"; }

{
  echo "# MAIA Phase 1 Terraform gate — measured evidence"
  echo "measured_at : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host        : $(uname -sm)"
  echo "branch      : $(git rev-parse --abbrev-ref HEAD)"
  echo "source_sha  : $(git rev-parse HEAD)"
  echo "method      : every number below was printed by a command executed at source_sha"
  echo "scope       : offline only — no Azure login, no plan against a subscription, no apply"
  echo "rule        : nothing here is copied from another document"
} >"$OUT"

section "Tool versions"
p "terraform : $(terraform version | head -1)"
p "trivy     : $(trivy --version | head -1)"
p "python    : $(python3 --version 2>&1)"

section "Worktree state before the run"
DIRTY="$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')"
p "tracked files changed (excluding untracked evidence/output): $DIRTY"
p "^^ 0 = the figures below describe a frozen SHA, not local edits"

section "Inventory (counted from disk, not read from a doc)"
p "terraform files     : $(find infra/terraform -name '*.tf' -not -path '*/.terraform/*' | wc -l | tr -d ' ')"
p "tftest files        : $(find infra/terraform -name '*.tftest.hcl' -not -path '*/.terraform/*' | wc -l | tr -d ' ')"
p "module versions.tf  : $(find infra/terraform/modules -name versions.tf | wc -l | tr -d ' ')"
p "module lock files   : $(find infra/terraform/modules -name .terraform.lock.hcl | wc -l | tr -d ' ')"
p "python test files   : $(ls infra/terraform/tests/*.py | wc -l | tr -d ' ')"
p "plan fixture        : $(find infra/terraform/tests/fixtures -name '*.json' | wc -l | tr -d ' ')"

section "Bicep frozen?"
CHANGED="$(git diff --name-only origin/main...HEAD -- 'infra/*.bicep' 'infra/modules/**/*.bicep' 'infra/parameters/**' | wc -l | tr -d ' ')"
p "Bicep files changed vs origin/main : $CHANGED"
p "^^ 0 = the Terraform port did not touch Bicep"

section "Module sources (cross-repo check)"
MODULE_SOURCES="$(grep -Rh '^[[:space:]]*source[[:space:]]*=' infra/terraform --include='*.tf' \
  | sed -E 's/^[[:space:]]*//' | sort -u | grep -v '^source  *= *"hashicorp/')"
p "distinct module source expressions:"
printf '%s\n' "$MODULE_SOURCES" | sed 's/^/  - /' >>"$OUT"
p "^^ every entry must start with ./ (repo-local). A git:: URL, a registry"
p "   module name or a path outside this repo is a Phase 1 failure."

section "Provider sources (required_providers — not module sources)"
PROVIDER_SOURCES="$(grep -Rh '^[[:space:]]*source[[:space:]]*=' infra/terraform --include='*.tf' \
  | sed -E 's/^[[:space:]]*//' | sort -u | grep '^source  *= *"hashicorp/')"
printf '%s\n' "$PROVIDER_SOURCES" | sed 's/^/  - /' >>"$OUT"
p "^^ pinned by versions.tf and frozen in .terraform.lock.hcl / modules/*/.terraform.lock.hcl"

section "Gate run: infra/terraform/scripts/phase1_check.sh (offline)"
(cd infra/terraform && PHASE1_LOG="$REPO/$OUT" ./scripts/phase1_check.sh) >/tmp/gate_ev.log 2>&1
GATE=$?
cat /tmp/gate_ev.log >>"$OUT"
p ""
p "gate exit code: $GATE  (0 = PASS)"

section "check_plan_invariants.py against the committed fixture"
(cd infra/terraform && python3 tests/check_plan_invariants.py tests/fixtures/plan.v1.json) >>"$OUT" 2>&1
p "exit code: $?"

section "check_plan_invariants.py against a live-captured plan.json (gitignored)"
(cd infra/terraform && python3 tests/check_plan_invariants.py plan.json) >>"$OUT" 2>&1
p "exit code: $?"
p "(plan.json exists only on this machine; it holds placeholder values, no secrets)"

section "probe_plan_controls.py — does EACH control bite, for its own reason?"
(cd infra/terraform && python3 tests/probe_plan_controls.py) >>"$OUT" 2>&1
p "exit code: $?"

section "policies/scan.sh — positive control, repo scan, ignore-set guard"
(cd infra/terraform && ./policies/scan.sh) >/tmp/scan_ev.log 2>&1
SCAN=$?
grep -E '^== |^   ->|^Policy scan' /tmp/scan_ev.log >>"$OUT"
p "exit code: $SCAN"

section "Fail-closed: an unreadable plan must be a verdict, never a traceback"
(cd infra/terraform && python3 tests/check_plan_invariants.py /tmp/no-such-plan.json) >>"$OUT" 2>&1
p "exit code: $?"
p "^^ a non-zero exit with no traceback on stdout/stderr is the intended contract"

section "Verdict"
if [ "$GATE" -eq 0 ]; then
  p "Phase 1 offline gate: PASS at $(git rev-parse HEAD)"
else
  p "Phase 1 offline gate: FAIL at $(git rev-parse HEAD)"
fi
p "Still NOT done for Phase 1 (stated so the pass is not over-read):"
p "  - Bicep is still CURRENT_CANONICAL_IAC; nothing is merged, applied or imported"
p "  - no Azure plan/apply ran here, so no resource existence is claimed"
p "  - remote state / OIDC / import are Phase 2 and Phase 3, out of scope for Phase 1"

echo "wrote $OUT"
exit "$GATE"