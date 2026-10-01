#!/usr/bin/env bash
# IaC validation for MAIA. Compiles, lints, checks compiled-template
# invariants, and negative-tests all of it.
#
# WHAT THIS SCRIPT DOES NOT DO: it never authenticates to Azure and never
# creates, updates or deletes a resource. A green run means "the templates
# are well-formed and the guards hold", NOT "the stack exists".
#
# No step below is wrapped in `|| true`, and no failure is downgraded to a
# warning to get a green run.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

RED=$'\033[31m'; GREEN=$'\033[32m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
FAILURES=0
pass() { printf '%s  PASS%s  %s\n' "$GREEN" "$OFF" "$1"; }
fail() { FAILURES=$((FAILURES + 1)); printf '%s  FAIL%s  %s\n' "$RED" "$OFF" "$1"; [[ $# -gt 1 ]] && printf '%s\n' "$2" | sed 's/^/        /'; }
section() { printf '\n%s-- %s%s\n' "$BOLD" "$1" "$OFF"; }

# WHY pin-check the CLI: `az bicep` and a standalone `bicep` on PATH can be
# different versions, and a version skew turns a green run into a lie.
if ! command -v bicep >/dev/null 2>&1; then
  printf '%sERROR%s  bicep CLI not found. Install: az bicep install\n' "$RED" "$OFF"
  exit 2
fi
printf 'bicep: %s\n' "$(bicep --version 2>/dev/null | head -1)"

ART="${TMPDIR:-/tmp}/maia-iac-art"
rm -rf "$ART"; mkdir -p "$ART"

# ---------------------------------------------------------------- 1. compile
# EVERY .bicep is compiled, not only the entrypoints: a module nothing
# references yet is still the design record for a later wave.
section "1. compile every template (errors AND warnings are fatal)"
while IFS= read -r f; do
  err="$ART/$(echo "$f" | tr '/.' '__').err"
  bicep build "$f" --outfile "$ART/$(echo "$f" | tr '/.' '__').json" 2>"$err"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    fail "compile $f" "$(head -3 "$err")"
  elif [[ -s "$err" ]]; then
    # Bicep exits 0 on linter warnings, so an empty-stderr check is the only
    # way "warnings are fatal" is actually true rather than merely claimed.
    fail "compile $f (warnings)" "$(head -3 "$err")"
  else
    pass "compile $f"
  fi
done < <(find . -name '*.bicep' -not -path './validate/*' | sort)

# ------------------------------------------------------------------ 2. params
section "2. committed parameter files resolve"
while IFS= read -r p; do
  if bicep build-params "$p" --outfile "$ART/$(basename "$p").json" \
       >"$ART/params.err" 2>&1; then
    pass "params $p"
  else
    fail "params $p" "$(head -3 "$ART/params.err")"
  fi
done < <(find . -name '*.bicepparam' -not -path './validate/*' | sort)

# ------------------------------------------- 3. compiled-template invariants
section "3. V1 security invariants on the COMPILED template"
bicep build ./main.bicep --outfile "$ART/main.v1.json" 2>"$ART/v1.err"
if [[ -s "$ART/v1.err" ]]; then
  fail "V1 compile clean" "$(head -3 "$ART/v1.err")"
else
  pass "V1 compile clean"
fi
if python3 ./check_invariants.py "$ART/main.v1.json" | sed 's/^/      /'; then
  pass "invariants  purgeProtection + rbacAuthorization held"
else
  fail "invariants" "see the FAIL line above with its JSON path"
fi
# ------------------------------------------------------- 4. negative controls
section "4. negative controls (each MUST fail, with the expected behaviour)"
python3 ./validate/negative-invariants/run_negative_controls.py >"$ART/nc.txt" 2>&1
bad=$(grep -cE 'exit=0|traceback=YES' "$ART/nc.txt" || true)
if [[ "$bad" -gt 0 ]]; then
  fail "invariant negative controls" "$bad case(s) did not fail cleanly"
  sed 's/^/      /' "$ART/nc.txt"
else
  pass "invariant negative controls"
  sed 's/^/      /' "$ART/nc.txt"
fi

# --------------------------------------------- 5. committed secret heuristic
section "5. no literal secret values in templates or params"
# Deliberately narrow: a broad /secret/ pattern matches every legitimate
# reference to a secret NAME, and a check that always fires gets disabled.
# -i is required: without it the check matched `password =` but missed
# DEMO_USER_A_PASSWORD, which is the convention this repo and Azure use.
leaked="$(grep -rniE "(password|clientSecret|accountKey|connectionString|sharedAccessKey)[[:space:]]*[:=][[:space:]]*['\"][^'\"]" \
  --include='*.bicep' --include='*.bicepparam' --exclude-dir=validate . || true)"
if [[ -n "$leaked" ]]; then
  fail "secret scan" "$leaked"
else
  pass "secret scan  no literal secret values"
fi

section "6. secret scanner negative control (must CATCH)"
fixture_hits="$(grep -rniE "(password|clientSecret|accountKey|connectionString|sharedAccessKey)[[:space:]]*[:=][[:space:]]*['\"][^'\"]" \
  ./validate/negative-secret 2>/dev/null || true)"
if [[ -n "$fixture_hits" ]]; then
  pass "secret scanner bites  uppercase convention detected"
else
  fail "secret scanner bites" "the uppercase fixture was NOT detected -- the detector is blind"
fi

section "7. no real environment identifiers in parameter files"
bad_ids="$(grep -rnE "param[[:space:]]+(tenantId|deployIdentityPrincipalId)[[:space:]]*=[[:space:]]*'[0-9a-fA-F-]{36}'" \
  --include='*.bicepparam' --exclude-dir=validate . \
  | grep -viE "'0{8}-0{4}-0{4}-0{4}-0{12}'" || true)"
if [[ -n "$bad_ids" ]]; then
  fail "placeholder ids" "a non-placeholder tenant/principal id is committed: $bad_ids"
else
  pass "placeholder ids  tenant and principal ids are zero-GUID"
fi

section "summary"
if [[ "$FAILURES" -eq 0 ]]; then
  printf '%s== IaC validation: ALL CHECKS PASSED ==%s\n' "$GREEN" "$OFF"
  printf 'No Azure resource was created, updated or deleted by this run.\n'
  exit 0
fi
printf '%s== IaC validation: %d CHECK(S) FAILED ==%s\n' "$RED" "$FAILURES" "$OFF"
exit 1