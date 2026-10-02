# Remote state for MAIA validation (transient enterprise validation env).
# Same contract as prod; distinct key so the transient env can be destroyed and
# its state discarded without touching prod. No credential values.
resource_group_name  = "rg-maia-tfstate"
storage_account_name = "sttfmaia"
container_name       = "tfstate"
key                  = "maia/validation.terraform.tfstate"

# WHY THERE IS NO `use_oidc` HERE — measured, not assumed.
#
# A static `use_oidc = true` makes the azurerm backend take the GitHub Actions
# OIDC path unconditionally. That path reads ACTIONS_ID_TOKEN_REQUEST_TOKEN,
# which only exists inside a GitHub runner, so a LOCAL `az login` +
# `terraform init` fails. Committing it would trade one broken path for another:
# CI works, the developer's machine does not.
#
# The supported mechanism is the ARM_* environment, which both paths set:
#   LOCAL : `az login` supplies the token; backend.hcl's use_azuread_auth picks
#           it up. Nothing else needed.
#   CI    : .github/workflows/azure-verify.yml exports
#           ARM_USE_OIDC=true, ARM_USE_AZUREAD_AUTH=true, ARM_CLIENT_ID,
#           ARM_TENANT_ID, ARM_SUBSCRIPTION_ID before `terraform init`.
#
# So this file carries only storage IDENTITY and LOCATION (which account, which
# container, which key). Authentication is per-environment, supplied by whoever
# is running it. One config, two paths, no second backend.
#
# tests/probe_no_client_secret.py rule S5 fails the build if `use_oidc` is ever
# hardcoded back into this file.
use_azuread_auth = true