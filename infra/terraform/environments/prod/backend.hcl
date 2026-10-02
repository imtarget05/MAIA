# Remote state for MAIA prod — PARTIAL CONFIG, no credential values.
#
# Used as:  terraform init -backend-config=environments/prod/backend.hcl -reconfigure
#
# No account key, no SAS token, no client secret. The backend data plane
# authenticates with an Entra token — from `az login` locally, from the GitHub
# Actions federated credential in CI. This file is safe to commit.
#
# WHY THERE IS NO `use_oidc` HERE: a static `use_oidc = true` forces the GitHub
# Actions OIDC path (ACTIONS_ID_TOKEN_REQUEST_TOKEN), which does not exist on a
# developer machine, so local `terraform init` would break. Authentication is
# supplied per-environment via the ARM_* environment instead. One config, two
# paths, no second backend. See validation/backend.hcl for the full rationale
# and probe rule S5.
#
# The storage account / resource group are created by
# scripts/bootstrap_azure.sh (tfstate is bootstrapped out of band — a backend
# cannot provision its own backend).
resource_group_name  = "rg-maia-tfstate"
storage_account_name = "sttfmaia"
container_name       = "tfstate"
key                  = "maia/prod.terraform.tfstate"

use_azuread_auth = true