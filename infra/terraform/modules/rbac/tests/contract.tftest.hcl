# Source-parity contract for the rbac module (TF-M11, mocked providers).
# Asserts the two V1 grants are emitted (KV Secrets User + deploy Contributor)
# and the empty-scope grants are suppressed — the Terraform mirror of Bicep's
# `if (!empty(...))` guards.

mock_provider "azurerm" {}

variables {
  scope_id                          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-maia-identity"
  key_vault_id                      = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-maia-identity/providers/Microsoft.KeyVault/vaults/kv-maia-01"
  key_vault_in_scope                = true
  app_managed_identity_principal_id = "11111111-1111-1111-1111-111111111111"
  deploy_identity_principal_id      = "22222222-2222-2222-2222-222222222222"
  key_vault_secrets_user_role_id    = "/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6"
  acr_pull_role_id                  = "/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
  storage_blob_data_role_id         = "/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
  contributor_role_id               = "/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c"
}

run "v1_grants_emitted" {
  command = plan

  variables {
    container_registry_id   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-maia-identity/providers/Microsoft.ContainerRegistry/registries/acr-maia-01"
    blob_storage_account_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-maia-identity/providers/Microsoft.Storage/storageAccounts/stmaia01"
  }

  assert {
    condition     = length(azurerm_role_assignment.key_vault_secrets_user) == 1
    error_message = "V1 lost the Key Vault Secrets User grant (conditional emission broke)."
  }

  # Singleton (no count): length() would count OBJECT ATTRIBUTES, not instances.
  # Assert on property identity instead; the reference itself only resolves if
  # the grant is actually in the plan.
  assert {
    condition     = azurerm_role_assignment.deploy_identity_contributor.role_definition_id == var.contributor_role_id
    error_message = "V1 lost the deploy-identity Contributor grant on the identity scope."
  }

  assert {
    condition     = azurerm_role_assignment.deploy_identity_contributor.principal_id == var.deploy_identity_principal_id
    error_message = "Deploy Contributor grant targets the wrong principal."
  }

  assert {
    condition     = length(azurerm_role_assignment.acr_pull) == 1
    error_message = "AcrPull not emitted when a registry id is supplied."
  }

  assert {
    condition     = length(azurerm_role_assignment.blob_data_contributor) == 1
    error_message = "Blob Data Contributor not emitted when an account id is supplied."
  }
}

run "empty_scope_grants_suppressed" {
  command = plan

  variables {
    container_registry_id   = null
    blob_storage_account_id = null
  }

  assert {
    condition     = length(azurerm_role_assignment.acr_pull) == 0
    error_message = "AcrPull emitted with no registry in scope (Bicep guard violated)."
  }

  assert {
    condition     = length(azurerm_role_assignment.blob_data_contributor) == 0
    error_message = "Blob Data Contributor emitted with no account in scope (Bicep guard violated)."
  }
}
