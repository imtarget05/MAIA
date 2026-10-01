# Role assignments for one scope of the MAIA stack, least-privilege and scoped
# to a single named resource. Port of infra/modules/rbac/main.bicep.
#
# WHY the deploy identity gets Contributor on this scope only: a
# subscription-scoped Contributor could attach its own role assignments and
# escalate to Owner. Scoped to one group/resource it cannot; the CI identity
# subscription-scoped Contributor could attach its own role assignments and
# escalate to Owner. Scoped to one group/resource it cannot; the CI identity
# can roll back this stack and nothing else.
#
# Conditional grants mirror Bicep's `if (!empty(...))` guards: the module never
# binds a role to a resource that is not there. V1 supplies only the Key Vault;
# registry and storage ids arrive with the waves that create them.

variable "scope_id" {
  description = "Scope for the deploy-identity Contributor grant (the identity resource group id)."
  type        = string
}

variable "key_vault_id" {
  description = "Resource id of the Key Vault. Used as the grant scope when key_vault_in_scope is true."
  type        = string
}

variable "key_vault_in_scope" {
  description = "Emit the Key Vault Secrets User grant. False when no vault is in scope (parity with Bicep `if (!empty(keyVaultName))`). Default matches V1, which always supplies a vault."
  type        = bool
  default     = true
}

variable "app_managed_identity_principal_id" {
  description = "Object id (principal id) of the user-assigned managed identity the MAIA workload runs as. The target of every runtime grant."
  type        = string
}

variable "deploy_identity_principal_id" {
  description = "Object id of the identity GitHub Actions deploys as. The target of the Contributor grant."
  type        = string
}

variable "key_vault_secrets_user_role_id" {
  description = "Full id of the Key Vault Secrets User role definition (tenant-level path)."
  type        = string
}

variable "acr_pull_role_id" {
  description = "Full id of the AcrPull role definition."
  type        = string
}

variable "storage_blob_data_role_id" {
  description = "Full id of the Storage Blob Data Contributor role definition."
  type        = string
}

variable "contributor_role_id" {
  description = "Full id of the Contributor role definition."
  type        = string
}

variable "container_registry_id" {
  description = "Full resource id of the container registry, or null when no registry is in scope (V1)."
  type        = string
  default     = null
}

variable "blob_storage_account_id" {
  description = "Full resource id of the blob storage account, or null when no account exists yet (V1)."
  type        = string
  default     = null
}
