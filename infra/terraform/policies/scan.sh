#!/usr/bin/env bash
# Policy scan for infra/terraform (Trivy).
#
# Two things must both be true, and a scan that only does the first proves
# nothing:
#
#   1. POSITIVE CONTROL — a deliberately weakened Key Vault MUST be reported.
#      Without this, a config that disables the scanner entirely would look
#      identical to a clean source tree, because both exit 0.
#   2. REPO SCAN — this repository's sources MUST pass with only the documented
#      exception in policies/trivyignore.
#   3. IGNORE-SET GUARD — the ignore file must contain exactly the documented
#      set, so adding a suppression is a visible change rather than a quiet one.
#
# Skips are refused: if Trivy is unavailable the scan fails loudly unless
# PHASE1_SKIP_TRIVY=1 is set deliberately. A silently-skipped security gate and
# a passing one print the same thing.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || exit 2

if ! command -v trivy >/dev/null 2>&1; then
  if [ "${PHASE1_SKIP_TRIVY:-0}" = "1" ]; then
    echo "SKIP policy scan: trivy is not installed and PHASE1_SKIP_TRIVY=1 was set deliberately."
    echo "      THIS IS AN ACCEPTED RISK FOR THIS RUN ONLY — the gate output above says nothing about policy."
    exit 0
  fi
  echo "FAIL policy scan: trivy is not installed."
  echo "      install: https://trivy.dev/latest/getting-started/installation/"
  echo "      or set PHASE1_SKIP_TRIVY=1 to acknowledge a run without the policy scan."
  exit 1
fi

FAILED=0
note() { printf '\n== %s\n' "$1"; }

# Severity covers MEDIUM as well: purge protection is only AZU-0016/MEDIUM, and
# gating HIGH+ alone would let a regress that weakens the vault go unreported.
SEVERITY="LOW,MEDIUM,HIGH,CRITICAL"

# --- 1. Positive control -----------------------------------------------------
note "positive control: a weakened vault must be reported"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cat >"$WORK/main.tf" <<'HCL'
resource "azurerm_key_vault" "weakened" {
  name                          = "kv-weakened-control"
  location                      = "eastasia"
  resource_group_name           = "rg-control"
  tenant_id                     = "00000000-0000-0000-0000-000000000000"
  sku_name                      = "standard"
  rbac_authorization_enabled    = false
  purge_protection_enabled      = false
  soft_delete_retention_days    = 7
  public_network_access_enabled = true
  network_acls {
    bypass         = "AzureServices"
    default_action = "Deny"
  }
}
HCL

# AZU-0016 (purge protection), NOT AZU-0013: using the suppressed ID as the
# control would let a checker that only ever suppresses that ID pass this test.
if trivy config -c "$HERE/trivy.yaml" --severity "$SEVERITY" --exit-code 1 \
    --skip-version-check "$WORK" >"$WORK/out.txt" 2>&1; then
  echo "   -> FAILED: the scanner reported no finding on a weakened vault, so a clean"
  echo "      repo scan below would prove nothing."
  FAILED=1
elif grep -q "AZU-0016" "$WORK/out.txt"; then
  echo "   -> ok: AZU-0016 reported"
else
  echo "   -> FAILED: a weakened vault was not reported as AZU-0016."
  grep -E "AZU-" "$WORK/out.txt" | head -5
  FAILED=1
fi

# Repo scan (documented exception only)
#
# `--tf-vars` pins the environment the scan evaluates: Trivy otherwise warns
# that no variable values were found and evaluates the HCL against defaults
# only. V1 uses `enable_private_network_for_vault = false` in every committed
# environment file, so prod is the posture being scanned; the private switch
# itself is covered by invariant KV2 in tests/check_plan_invariants.py.
note "repo scan (documented exception only)"
if trivy config -c "$HERE/trivy.yaml" --severity "$SEVERITY" --exit-code 1 \
    --ignorefile "$HERE/trivyignore" \
    --tf-vars "$ROOT/environments/prod/terraform.tfvars" \
    --skip-dirs .terraform --skip-version-check . ; then
  echo "   -> ok"
else
  echo "   -> FAILED"
  FAILED=1
fi

# --- 3. Ignore-set guard -----------------------------------------------------
note "ignore set is exactly the documented exception"
ACTUAL="$(grep -E '^[A-Za-z][A-Za-z0-9-]*$' "$HERE/trivyignore" | sort -u | tr '\n' ' ')"
EXPECTED="AZU-0013 "
if [ "$ACTUAL" = "$EXPECTED" ]; then
  echo "   -> ok: $ACTUAL"
else
  echo "   -> FAILED: ignore set is [$ACTUAL], expected [$EXPECTED]."
  echo "      Every suppression must carry a reason in policies/trivyignore and a"
  echo "      matching line in policies/README.md, or this guard blocks it."
  FAILED=1
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  echo "Policy scan: PASS"
else
  echo "Policy scan: FAIL"
fi
exit "$FAILED"
