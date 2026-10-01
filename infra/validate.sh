#!/usr/bin/env bash
# IaC validation for MAIA. Compiles, lints and negative-tests the Bicep.
#
# WHAT THIS SCRIPT DOES NOT DO: it never authenticates to Azure and never
# creates, updates or deletes a resource. There is no `az login`, no
# `az deployment ... create` and no `--what-if` anywhere below. A green run
# means "the templates are well-formed and the guards hold", NOT "the stack
# exists". Claiming a deployment on the basis of this script is exactly the
# overclaim the evidence rules forbid.
#
# It is the same script CI runs (.github/workflows/iac-validate.yml), so a
# local green and a CI green mean the same thing.
#
# Usage: infra/validate.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED=$'\033[31m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
BOLD=$'\033[1m'
OFF=$'\033[0m'

failures=0
pass() { printf '%s  PASS%s  %s\n' "$GREEN" "$OFF" "$1"; }
fail() {
  printf '%s  FAIL%s  %s\n' "$RED" "$OFF" "$1"
  if [[ -n "${2:-}" ]]; then printf '        %s\n' "$2"; fi
  failures=$((failures + 1))
}

# WHY this check instead of trusting the caller: `az bicep` and a standalone
# bicep on PATH can be different versions, and a version skew between the
# developer's machine and CI turns a green run into a lie.
if ! command -v bicep >/dev/null 2>&1; then
  printf '%sERROR%s  bicep CLI not found. Install: az bicep install\n' "$RED" "$OFF"
  exit 2
fi


# ---------------------------------------------------------------- 1. compile
# Every .bicep file is compiled on its own, not only the two entrypoints. A
# module nothing references yet (the V6 APIM and edge modules) is still worth
# compiling: it is the design record for later waves, and a module that does
# not compile is a wave that starts broken.
# WHY warnings are a hard failure, and how this detects them. `bicep build`
# writes the compiled ARM template to stdout and ALL diagnostics — warnings
# included — to stderr, and it exits 0 when there are warnings but no errors.
# An exit-code check therefore passes a file full of `no-unused-vars` and
# `no-unnecessary-dependson` findings, which would make this job decoration.
# So stdout is discarded, stderr is captured, and any line carrying a Bicep
# warning code fails the run. Measured on bicep 0.47.16, not assumed.
#
# The grep is anchored on the `Warning` keyword that precedes every linter
# diagnostic rather than on a fixed list of rule names, so a new linter rule is
# gated the day it is introduced without editing this script.
printf '%s-- 1. compile every template (errors AND warnings are fatal)%s\n' "$BOLD" "$OFF"
compile_failed=0
while IFS= read -r file; do
  if err="$(bicep build "$file" --stdout 2>&1 >/dev/null)"; then
    if grep -q "Warning" <<<"$err"; then
      fail "compile  $file" "compiled, but emitted a warning; warnings are a hard gate here:
$(grep "Warning" <<<"$err")"
      compile_failed=1
    else
      pass "compile  $file"
    fi
  else
    fail "compile  $file" "$err"
    compile_failed=1
  fi
done < <(find . -name '*.bicep' -not -path './validate/*' | sort)

# WHY a failed compile stops the run: the param and negative tests load these
# same templates. Running them against a template that does not compile would
# produce failures that look like guard regressions and bury the real one.
if [[ $compile_failed -ne 0 ]]; then
  printf '\n%sCompilation failed; stopping before the param tests.%s\n' "$RED" "$OFF"
  exit 1
fi

# ------------------------------------------------------------------ 2. params
# The committed dev parameter file must resolve. It carries placeholder values
# only; see the comment block in the file.
printf '\n%s-- 2. committed parameter files resolve%s\n' "$BOLD" "$OFF"
while IFS= read -r file; do
  if err="$(bicep build-params "$file" --stdout 2>&1 >/dev/null)"; then
    pass "params   $file"
  else
    fail "params   $file" "$err"
  fi
done < <(find . -name '*.bicepparam' -not -path './validate/*' | sort)

# -------------------------------------------------------------- 3. negative
# A negative fixture that FAILS is the point: it proves the guard still holds.
#
# WHY the expected error code is asserted rather than just "non-zero exit": a
# fixture fails for a typo in its `using` path just as readily as for the
# condition it exists to test, and both look like exit 1. Asserting the code
# means a broken fixture is reported as a broken fixture instead of silently
# counting as a passing negative test. This is not hypothetical — an earlier
# draft of these fixtures failed on BCP091 (file not found) and would have
# scored as a pass on exit code alone.
printf '%s-- 3. negative tests (each MUST fail, with the expected code)%s\n' "$BOLD" "$OFF"

NEGATIVE_CASES=(
  "validate/negative/missing-deploy-identity.bicepparam|BCP258|a required security parameter cannot be omitted"
  "validate/negative/principal-id-wrong-type.bicepparam|BCP033|a role-assignment principal id cannot be a non-string"
)

for entry in "${NEGATIVE_CASES[@]}"; do
  IFS='|' read -r file expected proves <<<"$entry"

  if [[ ! -f "$file" ]]; then
    fail "negative $file" "fixture is missing; a deleted negative test is a deleted guard"
    continue
  fi

  if err="$(bicep build-params "$file" --stdout 2>&1 >/dev/null)"; then
    fail "negative $file" "COMPILED, but must not. $proves"
  elif grep -q "$expected" <<<"$err"; then
    pass "negative $file [$expected] $proves"
  else
    fail "negative $file" "failed, but not with $expected - that is a failure for the wrong reason, not a pass. Got: $(head -1 <<<"$err")"
  fi
done

# ------------------------------------------------------- 4. known-weak pins
# These fixtures document a gap the type system does NOT catch, so they are
# expected to COMPILE. If one starts failing, the constraint moved into an
# `assert` and the fixture should be promoted to a real negative test.
printf '%s-- 4. known-weak pins (each MUST still compile)%s\n' "$BOLD" "$OFF"
weak_count=0
while IFS= read -r file; do
  weak_count=$((weak_count + 1))
  if err="$(bicep build-params "$file" --stdout 2>&1 >/dev/null)"; then
    pass "weak-pin $file"
  else
    fail "weak-pin $file" "no longer compiles. If that is because the constraint is now enforced, move the fixture to validate/negative/ and assert the error code. Got: $(head -1 <<<"$err")"
  fi
done < <(find validate/known-weak -name '*.bicepparam' 2>/dev/null | sort)
if [[ $weak_count -eq 0 ]]; then
  fail "weak-pin" "validate/known-weak/ is empty; the known gaps are no longer recorded anywhere"
fi


# ------------------------------------------------------------------ 5. secrets
# A Bicep file that ever holds a secret value is a secret in git history
# forever. The vault is created empty on purpose and values are written out of
# band, so a literal here is always a mistake.
printf '%s-- 5. no secret values in templates or params%s\n' "$BOLD" "$OFF"
# Deliberately narrow: a broad /secret/ pattern matches the many legitimate
# references to secret NAMES and to the Key Vault module, and a check that
# always fires eventually gets disabled.
#
# WHY -i (case-insensitive). Proven blind spot: this grep was case-SENSITIVE,
# so it matched `password = 'x'` but not `DEMO_USER_A_PASSWORD = 'x'` -- which
# is the exact naming convention this repo and Azure use for every secret
# variable (DEMO_USER_A_PASSWORD, JWT_SECRET_KEY, QDRANT_API_KEY). A control
# that is green while missing its own convention is worse than no control.
# Narrowness is preserved; only the case sensitivity is closed.
#
# WHY ./validate/* is excluded: that tree is negative-test FIXTURES. It must
# contain real secrets on purpose, so scanning it as if it were shipped IaC
# would make the suite permanently red. validate/negative-secret asserts below
# that the scanner still fires on those fixtures.
leaked="$(grep -rniE "(password|clientSecret|accountKey|connectionString|sharedAccessKey)[[:space:]]*[:=][[:space:]]*['\"][^'\"]" \
  --include='*.bicep' --include='*.bicepparam' --exclude-dir=validate . || true)"
if [[ -n "$leaked" ]]; then
  fail "secret scan" "$leaked"
else
  pass "secret scan  no literal secret values"
fi

# --------------------------------------------- 5b. the scanner must BITE
# A scanner that has never been shown to fail is an assumption, not a control.
# This runs the SAME detector over the fixture tree and requires it to fire.
# The fixture uses the repo's own uppercase convention, which is precisely
# what the case-sensitive version missed.
# ----------------------------------------------------------------------
printf '%s-- 5b. secret scanner negative control (must CATCH)%s\n' "$BOLD" "$OFF"
fixture_hits="$(grep -rniE "(password|clientSecret|accountKey|connectionString|sharedAccessKey)[[:space:]]*[:=][[:space:]]*['\"][^'\"]" \
  validate/negative-secret 2>/dev/null || true)"
if [[ -n "$fixture_hits" ]]; then
  pass "secret scanner bites  uppercase secret fixture is detected"
else
  fail "secret scanner bites" "the uppercase fixture in validate/negative-secret was NOT detected -- the detector is blind again"
fi

# A committed real parameter file is the other way secrets and tenant ids leak
# in. Two directories hold parameter files (params/ for V1, parameters/ for the
# V6 target) and an earlier revision of this check only looked at params/,
# which meant the real tenant id sitting in parameters/ passed unnoticed. Both
# are scanned now, and a non-placeholder tenant id fails the job outright.
printf '%s-- 6. no real environment identifiers in parameter files%s\n' "$BOLD" "$OFF"

# A zero GUID is the placeholder. Any other tenant-shaped value is real data.
tenant_leak="$(grep -rnE "param[[:space:]]+tenantId[[:space:]]*=[[:space:]]*'[0-9a-fA-F-]{36}'" \
  --include='*.bicepparam' . \
  | grep -viE "'0{8}-0{4}-0{4}-0{4}-0{12}'" || true)"
if [[ -n "$tenant_leak" ]]; then
  fail "tenant id" "a non-placeholder tenantId is committed. Replace it with the zero GUID and supply the real value out of band. Found: $tenant_leak"
else
  pass "tenant id  every tenantId is the zero-GUID placeholder"
fi

# A parameter file whose principal id is not the placeholder would name a real
# service principal, and role assignments bind to whatever it names.
principal_leak="$(grep -rnE "param[[:space:]]+deployIdentityPrincipalId[[:space:]]*=[[:space:]]*'[0-9a-fA-F-]{36}'" \
  --include='*.bicepparam' . \
  | grep -viE "'0{8}-0{4}-0{4}-0{4}-0{12}'" || true)"
if [[ -n "$principal_leak" ]]; then
  fail "principal id" "a non-placeholder deployIdentityPrincipalId is committed. Found: $principal_leak"
else
  pass "principal id  every deployIdentityPrincipalId is the zero-GUID placeholder"
fi

# Keep an inventory of what IS committed, so a reviewer can see the surface
# without listing files.
param_files="$(find params parameters -name '*.bicepparam' 2>/dev/null | sort | tr '\n' ' ')"
printf '        committed parameter files: %s\n' "${param_files:-none}"

# ------------------------------------------------- 7. security invariants
# Compile the V1 entrypoint to a real ARM template and assert the security
# properties on THAT artifact rather than grepping the .bicep source.
#
# WHY this step exists at all: every check above was green on 2026-10-01 with
# `enablePurgeProtection: false` in the Key Vault module. A compile proves the
# template is well-formed; it says nothing about whether the values in it are
# safe. This is the check that closes that gap, and it is the reason the known-
# weak fixture in validate/known-weak/ can eventually be deleted.
#
# WHY a compiled JSON and not a source grep: grepping the source would pass just
# as happily on a line inside a comment or a resource V1 never deploys. The
# compiled template is what Azure receives.
# The section header is printed by check_invariants.py itself, so this block
# only supplies the artifact and folds the exit code into the run total.
printf '\n'
invariant_dir="$(mktemp -d)"
# shellcheck disable=SC2064  # expand $invariant_dir now, not at trap time
trap "rm -rf '$invariant_dir'" EXIT

if bicep build main.bicep --outfile "$invariant_dir/main.v1.json" 2>/dev/null; then
  if python3 check_invariants.py "$invariant_dir/main.v1.json"; then
    : # check_invariants.py prints its own PASS/FAIL lines and sets the exit code
  else
    fail "invariants" "check_invariants.py reported a violation (see above)"
  fi
else
  # A compile failure here is already caught in step 1; this branch only needs
  # to say why step 7 could not run.
  fail "invariants" "could not compile main.bicep to JSON; see step 1"
fi

# --- 7b. traversal contract -----------------------------------------------
# WHY this is a separate, required step. check_invariants.py running without
# error proves only that it did not crash on THIS template. It does not prove
# the walk observed what it claims to check: a list-only walk skips Bicep's
# languageVersion 2.0 symbolic-name map entirely and still exits 0. The
# traversal suite asserts the nested resource was actually DISCOVERED, by name
# and path, and that malformed shapes fail closed with a JSON path.
if python3 scripts/test_checker_traversal.py check_invariants.py; then
  pass "traversal contracts  symbolic-name map and malformed shapes"
else
  fail "traversal contracts" "see the failing contract above"
fi

# The V6 target is compiled by step 1 but has no invariant assertions: it
# deploys no Key Vault, so there is nothing to claim. Stated here so the absence
# reads as a decision rather than an oversight.
printf '        main.v6-target.bicep: no invariant assertions (deploys no vault in this wave)\n'

# ------------------------------------------------------- checker traversal
# The checker above only exercises the real, compiled template. Its OWN
# traversal logic -- symbolic-name maps, path propagation, malformed shapes --
# is what silently stopped reporting vault invariants, so it needs its own
# controls. A non-zero exit must reach $failures; `|| true` here would turn the
# control into decoration while still printing reassuring PASS lines.
printf '\n-- checker traversal contracts --\n'
if python3 scripts/test_checker_traversal.py check_invariants.py; then
  :
else
  printf '%s  FAIL%s  checker traversal contracts failed\n' "$RED" "$OFF"
  failures=$((failures + 1))
fi

# ------------------------------------------------------------------ verdict
printf '\n'
if [[ $failures -eq 0 ]]; then
  printf '%s== IaC validation: ALL CHECKS PASSED ==%s\n' "$GREEN" "$OFF"
  printf 'No Azure resource was created, updated or deleted by this run.\n'
  exit 0
fi

printf '%s== IaC validation: %d CHECK(S) FAILED ==%s\n' "$RED" "$failures" "$OFF"
exit 1
