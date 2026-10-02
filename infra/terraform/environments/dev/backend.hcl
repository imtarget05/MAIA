# Remote state for MAIA dev. Same contract as prod; distinct key so dev churn
# can never corrupt prod state. No credential values.
#
# WHY THERE IS NO `use_oidc` HERE: a static `use_oidc = true` forces the
# GitHub Actions OIDC path (ACTIONS_ID_TOKEN_REQUEST_TOKEN), which does not
# exist on a developer machine. Authentication is supplied per-environment via
# the ARM_* environment instead — `az login` locally, exported ARM_* vars in CI.
# See validation/backend.hcl for the full rationale and probe rule S5.
resource_group_name  = "rg-maia-tfstate"
storage_account_name = "sttfmaia"
container_name       = "tfstate"
key                  = "maia/dev.terraform.tfstate"

use_azuread_auth = true