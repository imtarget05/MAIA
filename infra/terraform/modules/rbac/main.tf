# Key Vault Secrets User, not Secrets Officer: the app resolves its own secrets
# and must never be able to change them. A compromised container that can
# rotate JWT_SECRET_KEY can mint tokens for every user in the tenant.
resource "azurerm_role_assignment" "key_vault_secrets_user" {
  count = var.key_vault_in_scope ? 1 : 0

  scope              = var.key_vault_id
  role_definition_id = var.key_vault_secrets_user_role_id
  principal_id       = var.app_managed_identity_principal_id
  # Explicit: an assignment against a service principal without this field
  # fails with a principal-not-found error that reads like a replication
  # problem rather than a typing problem.
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = false
}

resource "azurerm_role_assignment" "acr_pull" {
  count = var.container_registry_id != null ? 1 : 0

  scope              = var.container_registry_id
  role_definition_id = var.acr_pull_role_id
  principal_id       = var.app_managed_identity_principal_id
  principal_type     = "ServicePrincipal"
}

# V4 extension point: emitted only once a storage account id is supplied.
resource "azurerm_role_assignment" "blob_data_contributor" {
  count = var.blob_storage_account_id != null ? 1 : 0

  scope              = var.blob_storage_account_id
  role_definition_id = var.storage_blob_data_role_id
  principal_id       = var.app_managed_identity_principal_id
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_assignment" "deploy_identity_contributor" {
  scope              = var.scope_id
  role_definition_id = var.contributor_role_id
  principal_id       = var.deploy_identity_principal_id
  principal_type     = "ServicePrincipal"
}
