#!/usr/bin/env bash
# Phase 1 (Terraform migration) verification gate for MAIA.
#
# WHY this script exists: the Phase 1 exit gate has several parts — source
# parity, `terraform test` green, and plan-JSON controls that actually bite.
# Running those by hand is how a step quietly gets skipped, and a skipped step
# looks exactly like a passing one (an empty `terraform test` run prints
# "Success! 0 passed, 0 failed"). This runs the whole set and fails on the first
# thing that fails.
#
# OFFLINE MODE (default — no credentials, no Azure contact):
#   fmt -check · init -backend=false · validate · terraform test (root + each
#   module) · negative-control probe over the committed plan fixture · Trivy
#   policy scan (positive control + repo scan + ignore-set guard)
#
# PLAN MODE (`--plan [env]` — REQUIRES Azure credentials, read-only):
#   additionally generates a real plan from an env tfvars, converts it with
#   `show -json`, and runs the invariant checker over THAT plan. This is the
#   only mode that contacts Azure, and it never applies. CI must not use it.
#
# Usage: ./scripts/phase1_check.sh [--plan [dev|validation|prod]]

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || exit 2

MODE="offline"
ENV_NAME="prod"
case "${1:-}" in
  --plan) MODE="plan"; ENV_NAME="${2:-prod}" ;;
  "") ;;
  *) echo "usage: $0 [--plan [dev|validation|prod]]" >&2; exit 2 ;;
esac

# The module list is explicit, not a glob: a new module that nobody added here
# must be a visible omission in review, not a silent zero-test pass.
TARGETS=("." "modules/identity" "modules/keyvault" "modules/rbac")

FAILED=0
LOG="${PHASE1_LOG:-}"

step() {
  printf '\n== %s\n' "$1"
  [ -n "$LOG" ] && printf '\n== %s\n' "$1" >>"$LOG"
}

run() {
  local label="$1"
  shift
  step "$label"
  if "$@" >/tmp/phase1_step.out 2>&1; then
    echo "   -> ok"
    [ -n "$LOG" ] && cat /tmp/phase1_step.out >>"$LOG"
  else
    echo "   -> FAILED"
    cat /tmp/phase1_step.out
    [ -n "$LOG" ] && cat /tmp/phase1_step.out >>"$LOG"
    FAILED=1
  fi
}

run_in() {
  local label="$1" dir="$2"
  shift 2
  step "$label ($dir)"
  if (cd "$dir" && "$@") >/tmp/phase1_step.out 2>&1; then
    echo "   -> ok"
    [ -n "$LOG" ] && cat /tmp/phase1_step.out >>"$LOG"
  else
    echo "   -> FAILED"
    cat /tmp/phase1_step.out
    [ -n "$LOG" ] && cat /tmp/phase1_step.out >>"$LOG"
    FAILED=1
  fi
}

# `terraform test` exits 0 when it found nothing to run: "Success! 0 passed,
# 0 failed" is indistinguishable from a real pass by exit code alone. So the
# run COUNT is asserted rather than trusted. This is what catches an empty
# tests/ directory, a renamed test directory, or a provider that never
# initialised — every one of which would otherwise report success.
run_test_in() {
  local dir="$1" line count
  step "terraform test ($dir)"
  if ! (cd "$dir" && terraform test -no-color) >/tmp/phase1_step.out 2>&1; then
    echo "   -> FAILED"
    cat /tmp/phase1_step.out
    [ -n "$LOG" ] && cat /tmp/phase1_step.out >>"$LOG"
    FAILED=1
    return 0
  fi
  line="$(grep -Eo 'Success! [0-9]+ passed, [0-9]+ failed' /tmp/phase1_step.out | tail -1)"
  count="$(printf '%s' "$line" | sed -E 's/Success! ([0-9]+) passed.*/\1/')"
  if [ -n "${count:-}" ] && [ "$count" -ge 1 ]; then
    echo "   -> ok: $line"
    [ -n "$LOG" ] && cat /tmp/phase1_step.out >>"$LOG"
  else
    echo "   -> FAILED: 0 assertions ran (summary: ${line:-<none>}). A gate that"
    echo "      passes while proving nothing is worse than one that fails."
    cat /tmp/phase1_step.out
    [ -n "$LOG" ] && cat /tmp/phase1_step.out >>"$LOG"
    FAILED=1
  fi
}

run "terraform version" terraform version
run "fmt -check -recursive" terraform fmt -check -recursive

for dir in "${TARGETS[@]}"; do
  run_in "init -backend=false" "$dir" terraform init -backend=false -input=false -no-color
done

run "validate" terraform validate -no-color

for dir in "${TARGETS[@]}"; do
  run_test_in "$dir"
done

run "plan-JSON negative controls" python3 tests/probe_plan_controls.py
run "policy scan (trivy)" bash "$ROOT/policies/scan.sh"

if [ "$MODE" = "plan" ]; then
  TFVARS="environments/$ENV_NAME/terraform.tfvars"
  if [ ! -f "$TFVARS" ]; then
    step "plan ($ENV_NAME)"
    echo "   -> FAILED: $TFVARS does not exist, so no invariant verdict can be claimed"
    FAILED=1
  else
    run "plan ($ENV_NAME)" terraform plan -input=false -lock=false -var-file="$TFVARS" -out=tfplan
    if [ -f tfplan ]; then
      step "show -json -> plan.json"
      if terraform show -json tfplan >plan.json; then
        echo "   -> ok"
      else
        echo "   -> FAILED"
        FAILED=1
      fi
      rm -f tfplan
    else
      step "show -json"
      echo "   -> FAILED: the plan produced no tfplan file"
      FAILED=1
    fi
    run "plan-JSON invariants (real plan)" python3 tests/check_plan_invariants.py plan.json
  fi
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  echo "Phase 1 gate: PASS (mode=$MODE)"
else
  echo "Phase 1 gate: FAIL (mode=$MODE)"
fi
exit "$FAILED"
