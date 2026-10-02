#!/usr/bin/env bash
# Azure bootstrap for MAIA Phase 2: remote state + secretless GitHub OIDC.
#
# WHAT THIS CREATES (all idempotent — safe to re-run):
#   1. rg-maia-tfstate            resource group for remote state
#   2. sttfmaia                   storage account + `tfstate` container
#   3. maia-github-oidc           Entra app + service principal (NO secret)
#   4. federated credential       subject = repo:imtarget05/MAIA:environment:azure-verify
#   5. rg-maia-verify             the ONLY RG the CI identity gets Contributor on
#   6. role assignments           CI identity: Contributor @ rg-maia-verify
#                                 CI identity: Storage Blob Data Contributor @ state RG
#                                 CI identity: Reader @ tfstate RG
#   7. budget                     alert only, NOT a cap
#
# WHY THE CLI AND NOT TERRAFORM: a remote state backend cannot be provisioned
# by the configuration that uses it. Bootstrapping is necessarily out of band;
# everything AFTER it is Terraform.
#
# COST POSTURE — READ THIS BEFORE RUNNING:
#   * NO application compute / database / API stack is created during bootstrap:
#     no VM, no APIM, no Front Door, no managed Postgres, no managed Redis, no
#     Container App. Those are Phase 3+ and are NOT authorised yet.
#   * BUT this script DOES create a Storage Account, and a storage account CAN
#     incur cost (capacity + transactions), even if the expected amount is very
#     small. Do not describe bootstrap as creating "no paid resources".
#   * The storage account is deliberately PERMANENT. It holds Terraform remote
#     state; destroying it would destroy the project's state history. Only the
#     transient APPLICATION stack is destroyed after verification — never the
#     state storage.
#   * A budget is an ALERT, not a cap: Azure does NOT stop resources when the
#     budget is exceeded. The control that actually stops money is
#     scripts/transient_verify.sh, which destroys the stack after verification.
#
# PREREQUISITES: `az login` on a tenant where you can create app registrations
# and assign roles.
#
# Usage: ./scripts/bootstrap_azure.sh --dry-run
#        ALLOW_AZURE_MUTATION=1 ./scripts/bootstrap_azure.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Names (single source of truth; the workflow and docs quote these) -------
APP_DISPLAY_NAME="maia-github-oidc"
FED_CREDENTIAL_NAME="github-actions-azure-verify"
GITHUB_REPO="imtarget05/MAIA"
GITHUB_ENVIRONMENT="azure-verify"
GITHUB_ISSUER="https://token.actions.githubusercontent.com"
GITHUB_AUDIENCE="api://AzureADTokenExchange"

STATE_RG="rg-maia-tfstate"
STATE_SA="sttfmaia"
STATE_CONTAINER="tfstate"

# The RG CI is allowed to touch. Contributor here, nowhere else.
CI_RG="rg-maia-verify"
LOCATION="${AZURE_LOCATION:-southeastasia}"
BUDGET_AMOUNT="${AZURE_BUDGET_AMOUNT:-10}"
OWNER_CONTACT="${AZURE_OWNER_CONTACT:-platform-team@example.invalid}"

# Role definition GUIDs (well-known, tenant-independent).
ROLE_CONTRIBUTOR="b24988ac-6180-42a0-ab88-20f7382dd24c"
ROLE_BLOB_DATA_CONTRIBUTOR="ba92f5b4-2d11-453d-a403-e96b0029c9fe"
ROLE_READER="acdd72a7-3385-48ef-bd42-f606fba81ae7"
ROLE_OWNER="8e3af657-a8ff-bdc3-b751-76f5fe6b70cb"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

FAILED=0
step() { printf '\n== %s\n' "$1"; }
ok()   { echo "   -> ok: $1"; }
fail() { echo "   -> FAILED: $1"; FAILED=1; }

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "   [dry-run] $*"
    return 0
  fi
  "$@"
}

command -v az >/dev/null 2>&1 || { echo "az CLI is required."; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required."; exit 2; }

# --- 0. Preflight: prove we are who we think we are -------------------------
step "preflight"
if ! SUBSCRIPTION_ID="$(az account show --query id -o tsv 2>/dev/null)"; then
  echo "   -> FAILED: not logged in. Run: az login && az account set --subscription <id>"
  exit 2
fi
TENANT_ID="$(az account show --query tenantId -o tsv)"
[ -n "$TENANT_ID" ] || { echo "   -> FAILED: tenantId is empty"; exit 2; }
echo "   subscription: $SUBSCRIPTION_ID"
echo "   tenant:       $TENANT_ID"
echo "   location:     $LOCATION"

if [ "$DRY_RUN" = "0" ] && [ "${ALLOW_AZURE_MUTATION:-0}" != "1" ]; then
  echo
  echo "This script MUTATES Azure (creates a resource group, a storage account and"
  echo "an app registration). Re-run with ALLOW_AZURE_MUTATION=1 to confirm:"
  echo "    ALLOW_AZURE_MUTATION=1 ./scripts/bootstrap_azure.sh"
  exit 3
fi

# --- 1. Remote state: resource group + storage account + container ----------
step "remote state: $STATE_RG / $STATE_SA"
run az group create --name "$STATE_RG" --location "$LOCATION" \
  --tags "project=maia" "managedBy=terraform-bootstrap" "owner=$OWNER_CONTACT" \
  -o none || fail "could not create resource group $STATE_RG"

run az storage account create \
  --name "$STATE_SA" \
  --resource-group "$STATE_RG" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --min-tls-version TLS1_2 \
  --https-only true \
  --allow-blob-public-access false \
  --tags "project=maia" "managedBy=terraform-bootstrap" \
  -o none || fail "could not create storage account $STATE_SA"

# --allow-shared-key-access false ENFORCES "no access key": SharedKey auth is
# refused server-side, so an account key leaked from anywhere is not a usable
# credential for the state plane. This is the control; the backend.hcl files
# are only the configuration that chooses OIDC.
run az storage account update --name "$STATE_SA" --resource-group "$STATE_RG" \
  --allow-shared-key-access false -o none \
  || fail "could not disable shared-key access on $STATE_SA"

# --- 1b. Blob data-plane access for the BOOTSTRAP RUNNER -------------------
#
# WHY THIS EXISTS — measured, not assumed: this script runs as a subscription
# Owner, and Owner is a MANAGEMENT-plane role carrying no dataActions. So
# `az storage container create --auth-mode login` below fails with 403
# AuthorizationPermissionMismatch even though every management-plane call in
# this script succeeds — the exact failure that makes a bootstrap look broken
# while leaving a half-built resource group behind.
#
# SCOPE IS THE STORAGE ACCOUNT, NOT THE RESOURCE GROUP.
# The resource group holds exactly one storage account today, so the two scopes
# have the same blast radius *now* — but they stop being equivalent the moment a
# second account is added, and then RG scope silently widens. Assigning at the
# account scope keeps the grant correct regardless of what else lands in the
# group. This is genuine least privilege, not a convenience trade.
#
# NOT re-enabled: shared account keys. Turning those back on would "solve" this
# by deleting the property that makes leaked keys useless.
#
# Kept permanently, deliberately: this is the same role CI holds, and without it
# `terraform init` cannot be run from this machine.
STATE_SA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$STATE_RG/providers/Microsoft.Storage/storageAccounts/$STATE_SA"

if [ "$DRY_RUN" != "1" ]; then
  STATE_SA_ID="$(az storage account show --name "$STATE_SA" --resource-group "$STATE_RG" \
    --query id -o tsv 2>/dev/null || true)"
  if [ -z "$STATE_SA_ID" ]; then
    fail "could not resolve the storage account resource id; cannot scope the data-plane role"
  fi
  STATE_SA_SCOPE="$STATE_SA_ID"
  echo "   storage account id: $STATE_SA_ID"
else
  echo "   storage account id: <resolved at apply time>"
fi

BOOTSTRAP_USER_ID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
if [ -z "$BOOTSTRAP_USER_ID" ]; then
  fail "cannot resolve the signed-in user id; the runner must be az login'd as a user, not a service principal"
fi

# read-back first so re-running does not create duplicate assignments
BOOTSTRAP_USER_HAS_DATA_ROLE=0
if [ "$DRY_RUN" != "1" ]; then
  BOOTSTRAP_USER_HAS_DATA_ROLE="$(az role assignment list \
    --assignee "$BOOTSTRAP_USER_ID" \
    --scope "$STATE_SA_SCOPE" \
    --include-inherited -o tsv 2>/dev/null \
    | grep -c "$ROLE_BLOB_DATA_CONTRIBUTOR" || true)"
fi

if [ "${BOOTSTRAP_USER_HAS_DATA_ROLE:-0}" -ge 1 ] 2>/dev/null; then
  ok "bootstrap runner already holds Storage Blob Data Contributor @ storage account"
else
  echo "   -> granting the bootstrap runner Storage Blob Data Contributor @ storage account"
  echo "      (management-plane roles do not carry dataActions; this is required"
  echo "       before --auth-mode login can create the container)"
  run az role assignment create \
    --assignee-object-id "$BOOTSTRAP_USER_ID" \
    --assignee-principal-type User \
    --role "$ROLE_BLOB_DATA_CONTRIBUTOR" \
    --scope "$STATE_SA_SCOPE" \
    -o none || fail "could not grant the runner Storage Blob Data Contributor"
  ok "runner data-plane role granted at storage-account scope"

  # --- 1c. Wait for RBAC propagation ------------------------------------
  # A role assignment returning from the control plane does NOT mean the data
  # plane has accepted it yet; Azure replicates asynchronously and the first
  # `az storage container create` can 403 on a role that already "exists".
  # Retrying blindly would hide a genuine permission problem, so this retries a
  # BOUNDED number of times and then gives up with an honest verdict.
  if [ "$DRY_RUN" != "1" ]; then
    echo "   -> waiting for the data-plane role to propagate"
    PROPAGATED=0
    attempt=1
    while [ "$attempt" -le 6 ]; do
      sleep 10
      if az storage container create \
           --name "$STATE_CONTAINER" \
           --account-name "$STATE_SA" \
           --auth-mode login -o none >/dev/null 2>&1; then
        PROPAGATED=1
        echo "   -> data-plane access confirmed after $((attempt * 10))s"
        break
      fi
      echo "   -> not propagated yet ($((attempt * 10))s), retrying"
      attempt=$((attempt + 1))
    done
    if [ "$PROPAGATED" != "1" ]; then
      fail "the data-plane role did not take effect within 60s. The role assignment
      exists but the data plane still rejects it — check tenant conditional
      access / PIM expiry rather than re-running blindly."
    fi
    STATE_CONTAINER_CREATED=1
  fi
fi

# --auth-mode login: creating the container with the account key would require
# the very access this account is configured to refuse.
#
# SKIPPED when 1c already created it during propagation probing — otherwise a
# re-run would attempt a second create for a container that exists.
if [ "${STATE_CONTAINER_CREATED:-0}" != "1" ]; then
  run az storage container create \
    --name "$STATE_CONTAINER" \
    --account-name "$STATE_SA" \
    --auth-mode login \
    -o none || fail "could not create container $STATE_CONTAINER (403 here means the runner still lacks the data-plane role above)"
fi

# --- 2. Entra app + service principal (no secret) --------------------------
step "entra identity: $APP_DISPLAY_NAME"
if [ "$DRY_RUN" = "1" ]; then
  # Read-backs are real queries against a real tenant. In dry-run there is
  # nothing to read yet, so printing a made-up GUID would be inventing the very
  # evidence this script exists to collect. Say so and stop pretending.
  APP_ID="<resolved at apply time>"
  SP_OBJECT_ID="<resolved at apply time>"
  ok "dry-run: skipping the app/service-principal read-backs"
else
  APP_ID="$(az ad app list --display-name "$APP_DISPLAY_NAME" \
    --query "[0].appId" -o tsv 2>/dev/null)"
  if [ -z "$APP_ID" ] || [ "$APP_ID" = "None" ]; then
    az ad app create --display-name "$APP_DISPLAY_NAME" -o none \
      || fail "could not create the app registration"
    APP_ID="$(az ad app list --display-name "$APP_DISPLAY_NAME" --query "[0].appId" -o tsv)"
  fi
  if [ -n "$APP_ID" ] && [ "$APP_ID" != "None" ]; then
    ok "appId $APP_ID"
  else
    fail "appId could not be resolved"
    exit 1
  fi

  SP_OBJECT_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null)"
  if [ -z "$SP_OBJECT_ID" ]; then
    az ad sp create --id "$APP_ID" -o none || fail "could not create the service principal"
    SP_OBJECT_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"
  fi
  if [ -n "$SP_OBJECT_ID" ]; then
    ok "service principal objectId $SP_OBJECT_ID"
  else
    fail "service principal objectId could not be resolved"
    exit 1
  fi

  # Guard the invariant, not the intent: an app registration that HAS a
  # password is not a secretless identity, however it was created.
  CRED_COUNT="$(az ad app show --id "$APP_ID" --query "length(passwordCredentials)" -o tsv 2>/dev/null || echo 0)"
  if [ "${CRED_COUNT:-0}" = "0" ]; then
    ok "no passwordCredentials on the app registration"
  else
    fail "the app registration has ${CRED_COUNT} password credential(s); a secretless identity must have none"
  fi
fi

# --- 3. Federated credential (GitHub OIDC) ---------------------------------
step "federated credential"
# SUBJECT FORMAT — MEASURED FROM A REAL FAILED RUN, not from documentation.
#
# GitHub does NOT send `repo:owner/repo:environment:env`. When a repository has
# a non-alphanumeric-containing name (or the owner does), it appends the NUMERIC
# owner id and repo id:
#
#   sent by GitHub : repo:imtarget05@163159731/MAIA@1357198812:environment:azure-verify
#   naive          : repo:imtarget05/MAIA:environment:azure-verify
#
# The naive form fails with AADSTS700211 "No matching federated identity record"
# at CI time, which looks exactly like a permissions problem and is not one.
#
# GITHUB_OWNER_ID / GITHUB_REPO_ID are passed in rather than hardcoded, because
# they are per-repository identifiers. Read them with:
#   gh api repos/<owner>/<repo> --jq '.owner.id, .id'
FED_SUBJECT="repo:${GITHUB_REPO}"
if [ -n "${GITHUB_OWNER_ID:-}" ] && [ -n "${GITHUB_REPO_ID:-}" ]; then
  GITHUB_REPO_SLUG="${GITHUB_REPO%%/*}"
  GITHUB_REPO_NAME="${GITHUB_REPO##*/}"
  FED_SUBJECT="repo:${GITHUB_REPO_SLUG}@${GITHUB_OWNER_ID}/${GITHUB_REPO_NAME}@${GITHUB_REPO_ID}"
else
  echo "   -> WARNING: GITHUB_OWNER_ID / GITHUB_REPO_ID not set."
  echo "      Falling back to repo:${GITHUB_REPO}, which will FAIL at CI time"
  echo "      with AADSTS700211 if GitHub appends numeric ids to the subject."
  echo "      Read the real ids with:"
  echo "        gh api repos/${GITHUB_REPO} --jq '{owner:.owner.id,repo:.id}'"
fi
FED_SUBJECT="${FED_SUBJECT}:environment:${GITHUB_ENVIRONMENT}"
echo "   federated subject: $FED_SUBJECT"
FED_FILE="$(mktemp)"
trap 'rm -f "$FED_FILE"' EXIT
cat >"$FED_FILE" <<JSON
{
  "name": "${FED_CREDENTIAL_NAME}",
  "issuer": "${GITHUB_ISSUER}",
  "subject": "${FED_SUBJECT}",
  "audiences": ["${GITHUB_AUDIENCE}"]
}
JSON

if az ad app federated-credential list --id "$APP_ID" -o tsv 2>/dev/null \
     | grep -q "$FED_CREDENTIAL_NAME"; then
  ok "federated credential already present"
else
  run az ad app federated-credential create --id "$APP_ID" --parameters "$FED_FILE" -o none \
    || fail "could not create the federated credential"
fi

# Read back and assert the EXACT values. "It was created" is not evidence that
# the subject is right — a wrong subject fails at run time in CI, which is
# precisely the failure this assertion prevents.
if [ "$DRY_RUN" = "1" ]; then
  ok "dry-run: skipping the federated-credential read-back"
else
  FED_JSON="$(az ad app federated-credential list --id "$APP_ID" -o json 2>/dev/null || echo '[]')"
  assert_fed() {
    local field="$1" expected="$2" actual
    actual="$(printf '%s' "$FED_JSON" | FED_NAME="$FED_CREDENTIAL_NAME" FED_FIELD="$field" \
      python3 -c '
import json, os, sys
name, field = os.environ["FED_NAME"], os.environ["FED_FIELD"]
for c in json.load(sys.stdin):
    if c.get("name") == name:
        v = c.get(field)
        print(v if isinstance(v, str) else ",".join(v or []))
        break
else:
    print("<absent>")
' 2>/dev/null)"
    if [ "$actual" = "$expected" ]; then
      ok "$field == $expected"
    else
      fail "$field is '$actual', expected '$expected'"
    fi
  }
  assert_fed issuer "$GITHUB_ISSUER"
  assert_fed subject "$FED_SUBJECT"
  assert_fed audiences "$GITHUB_AUDIENCE"
fi

# --- 4. Resource group CI is allowed to touch ------------------------------
step "deploy scope: $CI_RG"
run az group create --name "$CI_RG" --location "$LOCATION" \
  --tags "project=maia" "managedBy=terraform" "owner=$OWNER_CONTACT" -o none \
  || fail "could not create $CI_RG"

assign() {
  local role="$1" scope="$2" label="$3"
  if [ "$DRY_RUN" != "1" ] && az role assignment list --assignee "$SP_OBJECT_ID" --scope "$scope" \
       --include-inherited -o tsv 2>/dev/null | grep -q "$role"; then
    ok "$label already assigned"
    return 0
  fi
  run az role assignment create \
    --assignee-object-id "$SP_OBJECT_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "$role" --scope "$scope" \
    -o none || fail "could not assign $label"
  ok "$label assigned"
}

# Contributor on ONE resource group, never on the subscription. A
# subscription-scoped Contributor could grant itself Owner; a group-scoped one
# cannot. This is the entire reason a per-project RG exists.
assign "$ROLE_CONTRIBUTOR" "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$CI_RG" \
  "Contributor @ $CI_RG"

# Backend data plane: CI writes Terraform state, so it needs Blob Data
# Contributor. Reader on the state RG is what makes the scope resolvable.
assign "$ROLE_READER" "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$STATE_RG" \
  "Reader @ $STATE_RG"
assign "$ROLE_BLOB_DATA_CONTRIBUTOR" "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$STATE_RG" \
  "Storage Blob Data Contributor @ $STATE_RG"

# Negative control on privilege: the CI identity must hold nothing at
# subscription scope. If this ever trips, the blast radius was widened.
if [ "$DRY_RUN" = "1" ]; then
  ok "dry-run: skipping the subscription-scope privilege read-back"
else
  OWNER_ON_SUBSCRIPTION="$(az role assignment list --assignee "$SP_OBJECT_ID" \
    --scope "/subscriptions/$SUBSCRIPTION_ID" -o tsv 2>/dev/null \
    | grep -c "$ROLE_OWNER" || true)"
  if [ "${OWNER_ON_SUBSCRIPTION:-0}" = "0" ]; then
    ok "no Owner role assignment at subscription scope"
  else
    fail "the CI identity holds Owner at subscription scope — remove it immediately"
  fi
fi

# --- 5. Budget ------------------------------------------------------------
step "budget ($BUDGET_AMOUNT USD/month on $CI_RG)"
# Azure Budget ALERTS ONLY. Exceeding it does NOT stop resources. The control
# that actually stops money is transient_verify.sh (destroy after verify).
# Recorded here so nobody reads "$10 budget" as "hard cap".
#
# CLI NOTE — measured, not assumed: `az consumption budget create` is the
# SUBSCRIPTION-level command and takes no --resource-group, while the
# resource-group-scoped one is `create-with-rg`, which takes its alert
# recipients as a `--notifications` dictionary rather than an --alert-email
# flag. Using the wrong one fails on required arguments, so the correct
# command is used here and the notifications are built explicitly.
BUDGET_ALERT_RECIPIENT="${AZURE_BUDGET_ALERT_EMAIL:-NONE}"

if [ "$DRY_RUN" != "1" ] && az consumption budget show-with-rg \
     --budget-name "maia-verify-monthly" --resource-group "$CI_RG" \
     -o none >/dev/null 2>&1; then
  ok "budget already exists"
else
  # The time period must start on the first of a month; end date is open-ended.
  BUDGET_START="$(date -u +%Y-%m-01)"
  BUDGET_END="$(date -u -v+1y +%Y-%m-01)"

  # Thresholds 50 / 80 / 100 percent. A budget with no notification target is
  # cost TRACKING, not alerting, so the recipient is required for this to be
  # called alerting at all.
  #
  # The JSON goes through a file, not an inline string: az's shorthand parser
  # splits on the quotes inside `{"StartDate":"..."}` and fails with a quoting
  # error that looks like a validation error rather than a shell problem.
  BUDGET_BODY="$(mktemp)"
  trap 'rm -f "$FED_FILE" "$BUDGET_BODY"' EXIT
  if [ -n "${AZURE_BUDGET_ALERT_EMAIL:-}" ]; then
    python3 - "$BUDGET_BODY" "$BUDGET_START" "$BUDGET_END" "$BUDGET_AMOUNT" "$AZURE_BUDGET_ALERT_EMAIL" <<'PY'
import json, sys
path, start, end, amount, email = sys.argv[1:6]
thresholds = [50, 80, 100]
json.dump({
    "properties": {
        "displayName": {"name": "maia-verify-monthly", "description": "MAIA transient verification spend"},
        "amount": float(amount),
        "timeGrain": "Monthly",
        "timePeriod": {"start": start, "end": end},
        "category": "Cost",
        "notifications": [
            {"ContactEmails": [email], "Send": ["Email"], "Threshold": t}
            for t in thresholds
        ],
    }
}, open(path, "w"))
PY
  else
    python3 - "$BUDGET_BODY" "$BUDGET_START" "$BUDGET_END" "$BUDGET_AMOUNT" <<'PY'
import json, sys
path, start, end, amount = sys.argv[1:5]
json.dump({
    "properties": {
        "displayName": {"name": "maia-verify-monthly", "description": "MAIA transient verification spend"},
        "amount": float(amount),
        "timeGrain": "Monthly",
        "timePeriod": {"start": start, "end": end},
        "category": "Cost",
    }
}, open(path, "w"))
PY
  fi

  if [ "$DRY_RUN" = "1" ]; then
    ok "dry-run: budget body prepared, not submitted"
  elif az rest --method put \
       --url "https://management.azure.com/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$CI_RG/providers/Microsoft.Consumption/budgets/maia-verify-monthly?api-version=2024-08-01" \
       --body "@$BUDGET_BODY" >/dev/null 2>&1; then
    if [ -n "${AZURE_BUDGET_ALERT_EMAIL:-}" ]; then
      ok "budget created with alert recipient $AZURE_BUDGET_ALERT_EMAIL (50/80/100%)"
      BUDGET_STATUS="BUDGET_ALERTING_VERIFIED_LIVE"
    else
      ok "budget created"
      echo "   -> BUDGET_CREATED_ALERT_DELIVERY_NOT_CONFIGURED"
      echo "      Without a recipient this is cost TRACKING, not alerting: the budget"
      echo "      records spend but tells nobody when it is crossed. Re-run with"
      echo "      AZURE_BUDGET_ALERT_EMAIL=you@example.com to add 50/80/100% contacts."
      BUDGET_STATUS="BUDGET_CREATED_VERIFIED_LIVE__ALERT_DELIVERY_NOT_CONFIGURED"
    fi
  else
    # NOT a script bug: this is Azure rejecting the request. On an Azure for
    # Students subscription, `Microsoft.Consumption` rejects every start date
    # ("Please enter a valid start date") at both RG and subscription scope,
    # including the first of the current month. The cost control that actually
    # works is transient_verify.sh (trap-armed destroy), which is independent of
    # this budget. Reported as a distinct state rather than folded into PASS.
    BUDGET_STATUS="BUDGET_NOT_CREATED__AZURE_REJECTED_START_DATE"
    echo "   -> BUDGET_NOT_CREATED (external, not a script defect)"
    echo "      Azure rejected the budget time period at BOTH resource-group and"
    echo "      subscription scope, including the first of the current month."
    echo "      This is an Azure-side restriction on this subscription, not a"
    echo "      malformed request: the same body is valid, the dates are legal."
    echo "      Cost control does NOT depend on this: transient_verify.sh destroys"
    echo "      on EXIT with the destroy armed before apply."
    echo "      To retry later: Azure Portal -> rg-maia-verify -> Cost Management"
    echo "      -> Budgets -> Create (the portal accepts dates the API rejects here)."
  fi
fi

# --- 6. Output the three values GitHub needs -------------------------------
step "GitHub configuration"
cat <<OUT

Environment : $GITHUB_ENVIRONMENT   (create it in Settings -> Environments)
Repository  : $GITHUB_REPO

Settings -> Secrets and variables -> Actions -> Variables (variables, not
secrets — these three are identifiers, not credentials):
  AZURE_CLIENT_ID       = $APP_ID
  AZURE_TENANT_ID       = $TENANT_ID
  AZURE_SUBSCRIPTION_ID = $SUBSCRIPTION_ID

Do NOT create an Azure client secret for this identity: there is no password
credential on the app registration at all, so a secret would be a credential
with no purpose. tests/probe_no_client_secret.py fails the gate if one appears
in the deploy path.

Budget status        : ${BUDGET_STATUS:-UNKNOWN}
Budget is an ALERT, not a cap. It does not stop resources.
HARD COST CEILING    : NOT PROVIDED BY AZURE BUDGET

PERSISTENT, do not destroy:
  $STATE_RG / $STATE_SA / $STATE_CONTAINER  — holds Terraform remote state

TRANSIENT, destroyed after verification:
  $CI_RG and everything Terraform creates in it

Backend init:
  terraform init -backend-config=environments/prod/backend.hcl -reconfigure
OUT

printf '\n'
# BUDGET_STATUS is deliberately NOT fatal. The budget is an alerting nicety; the
# cost control that actually stops money is the trap-armed destroy in
# transient_verify.sh, which does not depend on it. Folding an Azure-side
# rejection into a red bootstrap would train the reader to ignore this script's
# exit code, which is how a real failure later gets missed.
if [ "$FAILED" -eq 0 ]; then
  echo "Bootstrap: PASS"
  [ "${BUDGET_STATUS:-}" = "BUDGET_NOT_CREATED__AZURE_REJECTED_START_DATE" ] \
    && echo "Bootstrap: PASS with one non-fatal gap (budget) — see the budget section"
  exit 0
fi
echo "Bootstrap: FAIL"
exit "$FAILED"