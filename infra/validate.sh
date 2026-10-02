#!/usr/bin/env bash
# IaC validation for MAIA — RE-ANCHORED FROM BICEP TO TERRAFORM.
#
# The source of truth for MAIA infrastructure is `infra/terraform/`. Every
# `.bicep` / `.bicepparam` file has been deleted; Bicep is no longer a supported
# IaC language here and `az bicep` is not installed by CI any more.
#
# WHAT THIS SCRIPT DOES NOT DO: it never authenticates to Azure and never
# creates, updates or deletes a resource. There is no `az login`, no
# `az deployment ... create` and no `terraform apply` anywhere below. `init` runs
# with `-backend=false`, so no remote state is contacted. A green run means "the
# configuration is well-formed and the controls hold", NOT "the stack exists".
# Claiming a deployment on the basis of this script is exactly the overclaim the
# evidence rules forbid.
#
# It is the same script CI runs (.github/workflows/iac-validate.yml), so a
# local green and a CI green mean the same thing.
#
# FAIL-CLOSED, NOT VACUOUS. A validator that finds nothing to check and exits 0
# is worse than no validator: it reports "PASS" for having verified nothing. So
# step 0 refuses to run when there is no Terraform at all, and every later step
# reports the concrete thing it exercised.
#
# Usage: infra/validate.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$SCRIPT_DIR/terraform"

RED=$'\033[31m'
GREEN=$'\033[32m'
BOLD=$'\033[1m'
OFF=$'\033[0m'

failures=0
pass() { printf '%s  PASS%s  %s\n' "$GREEN" "$OFF" "$1"; }
fail() {
  printf '%s  FAIL%s  %s\n' "$RED" "$OFF" "$1"
  if [[ -n "${2:-}" ]]; then printf '        %s\n' "$2"; fi
  failures=$((failures + 1))
}

# ------------------------------------------------- 0. the gate has something to check
printf '%s-- 0. terraform present (never vacuous)%s\n' "$BOLD" "$OFF"
if ! command -v terraform >/dev/null 2>&1; then
  fail "terraform CLI" "not found on PATH; refusing to report PASS having checked nothing"
  exit 1
fi
if [[ ! -d "$TF_DIR" ]]; then
  fail "terraform sources" "infra/terraform does not exist — the gate would be vacuous"
  exit 1
fi
# `|| true` because find on an unreadable path exits 1, and under `set -e
# -o pipefail` that would abort the script BEFORE the fail() message is
# printed. A gate that dies silently teaches people to re-run it.
tf_count="$(find "$TF_DIR" -name '*.tf' -not -path '*/.terraform/*' 2>/dev/null | wc -l | tr -d ' ' || true)"
if [[ "${tf_count:-0}" -eq 0 ]]; then
  fail "terraform sources" "no *.tf found under infra/terraform — the gate would be vacuous"
  exit 1
fi
pass "terraform sources  ${tf_count} *.tf under infra/terraform"

cd "$TF_DIR"

# ---------------------------------------------------------------- 1. formatting
printf '%s-- 1. terraform fmt%s\n' "$BOLD" "$OFF"
if out="$(terraform fmt -check -recursive 2>&1)"; then
  pass "fmt  clean"
else
  fail "fmt" "$out"
fi

# ------------------------------------------------- 2. init (no backend, no Azure)
printf '%s-- 2. init (backend disabled — no remote state is contacted)%s\n' "$BOLD" "$OFF"
if terraform init -backend=false -input=false >/dev/null 2>&1; then
  pass "init  providers resolved offline"
else
  fail "init" "terraform init -backend=false failed"
fi

# ---------------------------------------------------------------- 3. validate
printf '%s-- 3. terraform validate%s\n' "$BOLD" "$OFF"
if out="$(terraform validate 2>&1)"; then
  pass "validate  root configuration is valid"
else
  fail "validate" "$out"
fi

modules=()
while IFS= read -r dir; do
  [[ -n "$dir" ]] && modules+=("$dir")
done < <(find modules -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort)

module_fail=0
module_count=0
for m in "${modules[@]:-}"; do
  [[ -z "$m" ]] && continue
  module_count=$((module_count + 1))
  if ! (cd "$m" && terraform init -backend=false -input=false >/dev/null 2>&1 && terraform validate >/dev/null 2>&1); then
    fail "module $m" "init/validate failed"
    module_fail=$((module_fail + 1))
  fi
done
[[ "$module_fail" -eq 0 ]] && pass "validate  ${module_count} module(s) valid"
# ---------------------------------------------------------------- 4. terraform test
# The contract assertions are the real content of this gate: a configuration
# that parses can still wire a role id to nothing.
printf '%s-- 4. terraform test (contract assertions)%s\n' "$BOLD" "$OFF"
if out="$(terraform test 2>&1)"; then
  pass "test  root  $(printf '%s' "$out" | grep -oE '[0-9]+ passed' | head -1)"
else
  fail "test root" "$out"
fi

for m in "${modules[@]:-}"; do
  [[ -z "$m" ]] && continue
  if out="$(cd "$m" && terraform test 2>&1)"; then
    pass "test  $m  $(printf '%s' "$out" | grep -oE '[0-9]+ passed' | head -1)"
  else
    fail "test $m" "$out"
  fi
done

# ------------------------------------------- 5. plan controls must actually bite
# A checker that has never been shown to fail is an assumption, not a control.
# probe_plan_controls.py mutates a plan fixture many ways and feeds the checker
# unreadable/non-object JSON, requiring a fail-closed verdict and never a
# traceback.
printf '%s-- 5. plan-invariant controls must BITE%s\n' "$BOLD" "$OFF"
PY_BIN="${PYTHON:-}"
if [[ -z "$PY_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then PY_BIN=python3; fi
fi
if [[ -z "$PY_BIN" ]]; then
  fail "plan controls" "no python3 on PATH; cannot run the negative controls"
elif [[ ! -f tests/probe_plan_controls.py ]]; then
  fail "plan controls" "tests/probe_plan_controls.py is missing — the controls cannot be trusted"
else
  if out="$("$PY_BIN" tests/probe_plan_controls.py 2>&1)"; then
    pass "plan controls  $(printf '%s' "$out" | grep -oE '[0-9]+ passed, [0-9]+ failed' | tail -1)"
  else
    fail "plan controls" "$out"
  fi
fi

# ---------------------------------------------------------------- 6. secrets
# A .tf or .tfvars file that ever holds a secret value is a secret in git
# history forever. The vault is created empty on purpose and values are written
# out of band, so a literal here is always a mistake.
printf '%s-- 6. no secret values in terraform%s\n' "$BOLD" "$OFF"
# Deliberately narrow: a broad /secret/ pattern matches the many legitimate
# references to secret NAMES and to the Key Vault module, and a check that
# always fires eventually gets disabled. Case-insensitive on purpose — the
# uppercase convention this repo uses (JWT_SECRET_KEY, QDRANT_API_KEY) is
# exactly what a case-sensitive grep misses.
leaked="$(grep -rniE "(password|clientSecret|accountKey|connectionString|sharedAccessKey)[[:space:]]*[:=][[:space:]]*['\"][^'\"]" \
  --include='*.tf' --include='*.tfvars' --exclude-dir=.terraform . || true)"
if [[ -n "$leaked" ]]; then
  fail "secret scan" "$leaked"
else
  pass "secret scan  no literal secret values"
fi

# A committed real parameter value is the other way secrets and tenant ids leak
# in. A zero GUID is the placeholder; any other tenant-shaped value is real data.
printf '%s-- 7. no real environment identifiers in variable files%s\n' "$BOLD" "$OFF"
real_ids="$(grep -rniE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  --include='*.tfvars' --exclude-dir=.terraform . \
  | grep -viE '0000000-0000-0000-0000-000000000000' || true)"
if [[ -n "$real_ids" ]]; then
  fail "environment identifiers" "$real_ids"
else
  pass "environment identifiers  placeholders only"
fi

# ---------------------------------------------------------------- summary
printf '%s-- summary%s\n' "$BOLD" "$OFF"
if [[ "$failures" -eq 0 ]]; then
  printf '%sIaC validation: ALL CHECKS PASSED%s (terraform — no Azure mutation)\n' "$GREEN" "$OFF"
  exit 0
fi
printf '%sIaC validation: %d CHECK(S) FAILED%s\n' "$RED" "$failures" "$OFF"
exit 1