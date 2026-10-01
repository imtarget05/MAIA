# MAIA — Terraform canonical IaC (Phase 1 parity migration).
#
# MIGRATION STATUS: MIGRATION_CANDIDATE. Bicep (`infra/*.bicep`) remains
# CURRENT_CANONICAL_IAC until this reaches verified source parity and is merged.
# Nothing here is applied to Azure during Phase 1 (source parity only).
#
# Provider versions are pinned here and the resolved versions are frozen in
# .terraform.lock.hcl, which IS committed. azuread is required, not optional:
# MAIA's canonical V1 unit creates an Entra application registration
# (infra/modules/identity/main.bicep), which azurerm cannot express.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
  }
}
