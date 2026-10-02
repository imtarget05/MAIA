#!/usr/bin/env bash
# Transient verification deploy: apply -> verify -> DESTROY.
#
# WHY TRANSIENT: the alternative is leaving a live stack running over a weekend
# and calling "it deployed" a verification. Rule 15 of the program roadmap
# explicitly permits expensive resources to be transient, and rule 11 requires
# destroy before a phase can close.
#
# THE COST CONTROL IS THIS SCRIPT, NOT THE BUDGET. An Azure budget only alerts.
# So the destroy here is not best-effort cleanup — it is the budget mechanism:
#
#   * `trap ... EXIT` destroys on success, on failure, on Ctrl-C, and on any
#     unexpected exit. There is no path through this script that applies
#     without arming the destroy first.
#   * --keep is opt-in and requires ALSO_KEEP_RUNNING=1, so "leave it up to look
#     at it tomorrow" is a deliberate, greppable act and not an accident.
#
# Usage:
#   ./scripts/transient_verify.sh validation
#   KEEP=1 ALSO_KEEP_RUNNING=1 ./scripts/transient_verify.sh validation
#
# Requires: az login (or GitHub OIDC — see .github/workflows/azure-verify.yml),
# an initialised remote backend, and rg-maia-verify to exist.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || exit 2

ENV_NAME="${1:-validation}"
TFVARS="environments/$ENV_NAME/terraform.tfvars"
BACKEND="environments/$ENV_NAME/backend.hcl"

[ -f "$TFVARS" ]  || { echo "no such environment: $TFVARS"; exit 2; }
[ -f "$BACKEND" ] || { echo "no such backend config: $BACKEND"; exit 2; }

# deploy_identity_principal_id must be a REAL object id, or the Contributor
# assignment is created against the zero GUID and the stack is subtly wrong in
# a way the plan invariants cannot see (they check the role, not the principal).
DEPLOY_ID="$(grep -E '^[[:space:]]*deploy_identity_principal_id' "$TFVARS" \
  | sed -E 's/.*=[[:space:]]*"([^"]*)".*/\1/')"
if [ -z "${DEPLOY_ID:-}" ] || echo "$DEPLOY_ID" | grep -qi "^00000000-0000-0000-0000-000000000000$"; then
  echo "REFUSING to apply: $TFVARS still has the zero-GUID placeholder for"
  echo "deploy_identity_principal_id. Resolve it first:"
  echo "  az ad sp show --id <client-id-of-maia-github-oidc> --query id -o tsv"
  exit 2
fi

KEEP="${KEEP:-0}"
if [ "$KEEP" = "1" ] && [ "${ALSO_KEEP_RUNNING:-0}" != "1" ]; then
  echo "REFUSING KEEP=1 without ALSO_KEEP_RUNNING=1. Leaving resources up bills"
  echo "money for as long as they exist; make it a two-key decision on purpose."
  exit 2
fi

EVIDENCE_DIR="${EVIDENCE_DIR:-$ROOT/../../../docs/evidence/terraform-verify/$ENV_NAME}"
mkdir -p "$EVIDENCE_DIR"

step() { printf '\n== %s\n' "$1"; }
FAILED=0
DESTROY_ARMED=0

# --- The destroy arming, BEFORE anything is applied ------------------------
# If this trap were registered after `apply`, a failure between the two would
# leave a paid stack running with no cleanup path.
destroy() {
  local rc=$?
  if [ "$DESTROY_ARMED" != "1" ]; then
    return "$rc"
  fi
  if [ "$KEEP" = "1" ]; then
    echo
    echo "== destroy SKIPPED (KEEP=1, ALSO_KEEP_RUNNING=1 was set)."
    echo "   The stack is STILL BILLING. Destroy it with:"
    echo "   cd $ROOT && terraform destroy -var-file=$TFVARS"
    return "$rc"
  fi
  echo
  step "destroy (this is the cost control, not an afterthought)"
  terraform destroy -auto-approve -input=false -lock-timeout=5m \
    -var-file="$TFVARS" 2>&1 | tee "$EVIDENCE_DIR/destroy.log"
  local drc=${PIPESTATUS[0]}
  if [ "$drc" -ne 0 ]; then
    echo "   -> FAILED: destroy did not complete. Resources may still be billing."
    echo "      Re-run: cd $ROOT && terraform destroy -var-file=$TFVARS"
    FAILED=1
  else
    echo "   -> ok: stack destroyed"
  fi
  return "$rc"
}
trap destroy EXIT

# --- 1. Backend ------------------------------------------------------------
step "backend init ($BACKEND)"
terraform init -input=false -reconfigure -backend-config="$BACKEND" \
  2>&1 | tee "$EVIDENCE_DIR/init.log" || { echo "backend init failed"; exit 1; }

# Prove the backend really is remote. A silent fallback to local state would
# mean every later "apply verified" claim is about an environment nobody else
# can see.
BACKEND_STATE_OK=failed
terraform state pull >/dev/null 2>&1 && BACKEND_STATE_OK=ok
step "backend read-back"
{
  echo "environment: $ENV_NAME"
  echo "backend_config: $BACKEND"
  echo "state_pull: $BACKEND_STATE_OK"
  echo "terraform_version: $(terraform version | head -1)"
  echo "captured_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} | tee "$EVIDENCE_DIR/environment.txt"
[ "$BACKEND_STATE_OK" = "ok" ] || { echo "   -> FAILED: remote state is not readable"; exit 1; }

# --- 2. Plan, before anything is created -----------------------------------
step "plan ($ENV_NAME)"
terraform plan -input=false -lock=false -var-file="$TFVARS" -out=tfplan \
  2>&1 | tee "$EVIDENCE_DIR/plan.log"
[ -f tfplan ] || { echo "   -> FAILED: no plan produced"; exit 1; }

terraform show -json tfplan >plan.json
python3 tests/check_plan_invariants.py plan.json | tee "$EVIDENCE_DIR/plan-invariants.txt"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "   -> FAILED: plan invariants"; exit 1; }

# What is about to be billed, printed so a reviewer sees it now rather than on
# an invoice later.
step "resources about to be created"
terraform show -json tfplan >"$EVIDENCE_DIR/plan.json"
python3 - "$EVIDENCE_DIR/plan.json" <<'PY' | tee "$EVIDENCE_DIR/plan-resources.txt"
import json, sys
root = json.load(open(sys.argv[1]))["planned_values"]["root_module"]
for r in root.get("resources", []):
# --- 3. Apply --------------------------------------------------------------
step "apply ($ENV_NAME)"
DESTROY_ARMED=1
terraform apply -input=false -auto-approve -var-file="$TFVARS" \
  2>&1 | tee "$EVIDENCE_DIR/apply.log"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "   -> FAILED: apply. The EXIT trap will still destroy."
  FAILED=1
  exit 1
fi
echo "   -> ok: applied"

# --- 4. Verify what actually exists ---------------------------------------
# A green apply is not a verification. These read the LIVE control plane, so a
# resource that applied but is not actually serving is visible here.
step "live verification (read-back from Azure)"
az account show --query '{subscription:name, tenant:tenantId, state:state}' -o json \
  | tee "$EVIDENCE_DIR/az-account.json"

RG_NAME="$(grep -E '^[[:space:]]*identity_resource_group_name' "$TFVARS" \
  | sed -E 's/.*=[[:space:]]*"([^"]*)".*/\1/')"
if az group show -n "$RG_NAME" -o none >/dev/null 2>&1; then
  echo "   -> ok: resource group $RG_NAME exists"
else
  echo "   -> FAILED: resource group $RG_NAME does not exist after apply"
  FAILED=1
fi

az resource list --resource-group "$RG_NAME" -o table \
  2>/dev/null | tee "$EVIDENCE_DIR/resource-inventory.txt"

# Purge protection is the property that turns an accidental secret delete into
# a ticket instead of an outage. Assert it from the live vault, not from HCL.
KV_NAME="$(grep -E '^[[:space:]]*key_vault_name' "$TFVARS" \
  | sed -E 's/.*=[[:space:]]*"([^"]*)".*/\1/')"
if az keyvault show -n "$KV_NAME" -o none >/dev/null 2>&1; then
  PURGE="$(az keyvault show -n "$KV_NAME" --query enablePurgeProtection -o tsv)"
  if [ "$PURGE" = "true" ]; then
    echo "   -> ok: Key Vault $KV_NAME has purge protection"
  else
    echo "   -> FAILED: purge protection is off on $KV_NAME"
    FAILED=1
  fi
  az keyvault secret list --vault-name "$KV_NAME" -o table \
    >"$EVIDENCE_DIR/keyvault-secrets.txt" 2>&1
else
  echo "   -> FAILED: Key Vault $KV_NAME does not exist after apply"
  FAILED=1
fi

# State must be in the container, not on this machine. Local state files are a
# failed verification, so their presence is itself a finding.
step "state locality"
if ls ./*.tfstate* >/dev/null 2>&1; then
  echo "   -> FAILED: a local .tfstate exists. The backend is not being used."
  ls -la ./*.tfstate*
  FAILED=1
else
  echo "   -> ok: no local state file (state is remote)"
fi

rm -f tfplan

step "post-apply cost check"
if az consumption usage list --resource-group "$RG_NAME" \
     --start-date "$(date -u -v-1d +%Y-%m-%d)" --end-date "$(date -u +%Y-%m-%d)" \
     -o table >"$EVIDENCE_DIR/usage.txt" 2>&1; then
  echo "   usage written to $EVIDENCE_DIR/usage.txt"
else
  echo "   -> note: consumption usage unavailable (needs: az extension add --name consumption)"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  echo "Transient verification ($ENV_NAME): PASS — evidence in docs/evidence/terraform-verify/$ENV_NAME"
else
  echo "Transient verification ($ENV_NAME): FAIL — see the logs above"
fi
exit "$FAILED"
    print(f"   {r['type']}.{r['name']}")
for c in root.get("child_modules", []):
    for r in c.get("resources", []):
        print(f"   {r['type']}.{r['name']}   (module {c.get('address')})")
PY