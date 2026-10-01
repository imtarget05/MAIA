# Remote state backend contract (Phase 2 wires the actual backend).
#
# The block below is INTENTIONALLY COMMENTED OUT. Phase 1 is source parity and
# must run offline (`terraform init` resolves providers only, no Azure). An
# active `backend "azurerm"` block would force a remote-state connection at
# init time, which Phase 1 is not authorised to create or contact.
#
# Phase 2 will activate a backend of this shape, with per-environment keys so
# that dev / validation / prod never share a state file:
#
#   terraform {
#     backend "azurerm" {
#       resource_group_name  = "rg-tfstate-maia"        # created in Phase 2
#       storage_account_name = "<tfstate storage account>"
#       container_name       = "tfstate"
#       key                  = "maia/<environment>.terraform.tfstate"
#     }
#   }
#
# Values are supplied at `terraform init` time (CLI `-backend-config`, or the
# environments/<env>/backend.hcl convention), never hardcoded here, and no
# credential is ever written into this file. Blob leases provide state locking.
#
# State is NEVER committed. See infra/terraform/.gitignore.
