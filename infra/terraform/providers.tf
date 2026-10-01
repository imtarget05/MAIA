# Provider configuration for the MAIA stack.
#
# No subscription_id / tenant_id / client_id / client_secret is written here.
# Authentication is the environment's job (local `az login`, or GitHub OIDC in
# Phase 2). Terraform must never carry a long-lived credential in source, and
# nothing in this file authenticates to Azure during Phase 1.

provider "azurerm" {
  features {
    key_vault {
      # MATCHES BICEP SEMANTICS (infra/modules/keyvault/main.bicep):
      # enableSoftDelete = true, enablePurgeProtection = true.
      # A destroy must NOT purge the vault, because purge protection exists to
      # make an accidental delete recoverable. Recovering a soft-deleted vault
      # of the same name is therefore allowed.
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }

    resource_group {
      # A resource group with live resources is not silently deleted. This is
      # the Terraform-side equivalent of the Bicep comment on resourceGroups.bicep:
      # the deploy identity can destroy this stack and nothing outside it.
      prevent_deletion_if_contains_resources = true
    }
  }
}

provider "azuread" {}
