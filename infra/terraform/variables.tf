# Input contract for the MAIA root module. Mirrors the parameter contract of
# infra/main.bicep (V1): no secret VALUES anywhere, only names/references.

variable "environment_name" {
  description = "Short environment name (dev|validation|prod). Drives the shared tag set."
  type        = string
}

variable "location" {
  description = "Primary Azure region. The managed identity must sit in the same region as the workload that authenticates with it."
  type        = string
}

variable "tenant_id" {
  description = "Directory (tenant) id that owns the Key Vault and the Entra registration. Use the zero-GUID placeholder until an operator supplies the real value."
  type        = string
}

variable "owner_contact" {
  description = "Owner contact recorded in the tag set of every resource. Must be a monitored mailbox."
  type        = string
}

variable "identity_resource_group_name" {
  description = "Name of the resource group holding the managed identity, the Entra registration and the Key Vault."
  type        = string
}

variable "managed_identity_name" {
  description = "Name of the user-assigned managed identity the MAIA workload runs as."
  type        = string
}

variable "entra_application_name" {
  description = "Tenant-unique name of the Entra application registration. No spaces."
  type        = string
}

variable "web_redirect_uris" {
  description = "Redirect URIs on the Entra `web` platform. Empty in V1 (no gateway serves them yet)."
  type        = list(string)
  default     = []
}

variable "spa_redirect_uris" {
  description = "Redirect URIs on the Entra `spa` platform (public client, PKCE only). Empty in V1."
  type        = list(string)
  default     = []
}

variable "key_vault_name" {
  description = "Globally unique Key Vault name."
  type        = string
}

variable "enable_private_network_for_vault" {
  description = "True when the vault is reachable only through a private endpoint. False in V1 (a Deny default with no private endpoint would lock the workload out)."
  type        = bool
  default     = false
}

variable "deploy_identity_principal_id" {
  description = "Object id of the identity GitHub Actions deploys as. It receives Contributor on the identity resource group and nothing else."
  type        = string
}

variable "soft_delete_retention_in_days" {
  description = "Days a soft-deleted secret is recoverable for. 90 is the platform maximum."
  type        = number
  default     = 90
}

variable "container_registry_id" {
  description = "Full resource id of the container registry, or null when no registry is in scope (V1). Controls whether the AcrPull assignment is emitted."
  type        = string
  default     = null
}

variable "blob_storage_account_id" {
  description = "Full resource id of the blob storage account, or null when no account exists yet (V1). Controls whether the Blob Data Contributor assignment is emitted."
  type        = string
  default     = null
}
