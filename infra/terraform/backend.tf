# Remote state backend (Phase 2).
#
# The backend block below is PARTIAL CONFIG on purpose: every attribute lives in
# environments/<env>/backend.hcl (or `-backend-config` flags), never here. That
# keeps the source tree credential-free by construction — there is no line in
# this repository that a leaked git clone turns into a usable credential.
#
#   terraform {
#     backend "azurerm" {}
#   }
#
# Then per environment:
#
#   terraform init \
#     -backend-config=environments/prod/backend.hcl \
#     -reconfigure
#
# environments/prod/backend.hcl pins key = "maia/prod.terraform.tfstate", so dev /
# validation / prod never share a state file. Blob leases provide state locking.
#
# AUTHENTICATION IS ENTRA ID / OIDC, NOT AN ACCESS KEY.
# `use_oidc = true` + `use_azuread_auth = true` in every backend.hcl means the
# backend data plane authenticates with a token, so no account key is ever
# needed or stored. Locally the token comes from `az login`; in GitHub Actions
# it comes from the federated credential minted from the Actions OIDC token
# (see scripts/bootstrap_azure.sh and .github/workflows/azure-verify.yml).
# Consequence: the identity needs `Storage Blob Data Contributor` on the state
# container, NOT `Storage Account Contributor` — role assignment is out of band
# for the backend itself, which is why bootstrap_azure.sh does it with the CLI.
#
# WHY THIS IS STILL SAFE FOR THE OFFLINE GATE: scripts/phase1_check.sh inits with
# `-backend=false`, which skips backend initialisation entirely, so a backend
# block that needs Azure does not make the offline gate depend on Azure.
#
# State is NEVER committed. See infra/terraform/.gitignore.

terraform {
  backend "azurerm" {}
}
