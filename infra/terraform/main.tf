# Root composition: the V1 deployment unit (identity resource group + the
# identity, keyvault and rbac module instances), mirroring infra/main.bicep.
#
# Deployment order is not arbitrary: the resource group must exist before any
# group-scoped module, and RBAC is wired last so a partial failure leaves a
# stack with no privileges rather than a privileged stack with no identity.

resource "azurerm_resource_group" "identity" {
  name     = var.identity_resource_group_name
  location = var.location
  tags     = local.tags
}

module "identity" {
  source = "./modules/identity"

  resource_group_name    = azurerm_resource_group.identity.name
  location               = var.location
  environment_name       = var.environment_name
  managed_identity_name  = var.managed_identity_name
  entra_application_name = var.entra_application_name
  web_redirect_uris      = var.web_redirect_uris
  spa_redirect_uris      = var.spa_redirect_uris
  owner_contact          = var.owner_contact
  tags                   = local.tags
}

module "keyvault" {
  source = "./modules/keyvault"

  resource_group_name = azurerm_resource_group.identity.name

  location                      = var.location
  tenant_id                     = var.tenant_id
  environment_name              = var.environment_name
  key_vault_name                = var.key_vault_name
  owner_contact                 = var.owner_contact
  enable_private_network        = var.enable_private_network_for_vault
  soft_delete_retention_in_days = var.soft_delete_retention_in_days
  tags                          = local.tags

  depends_on = [azurerm_resource_group.identity]
}

module "rbac" {
  source = "./modules/rbac"

  scope_id                          = azurerm_resource_group.identity.id
  key_vault_id                      = module.keyvault.key_vault_id
  app_managed_identity_principal_id = module.identity.managed_identity_principal_id
  deploy_identity_principal_id      = var.deploy_identity_principal_id
  key_vault_secrets_user_role_id    = local.key_vault_secrets_user_role_id
  acr_pull_role_id                  = local.acr_pull_role_id
  storage_blob_data_role_id         = local.storage_blob_data_role_id
  contributor_role_id               = local.contributor_role_id
  container_registry_id             = var.container_registry_id
  blob_storage_account_id           = var.blob_storage_account_id

  depends_on = [module.keyvault]
}
