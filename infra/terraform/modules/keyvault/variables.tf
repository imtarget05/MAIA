# Key Vault for MAIA's runtime secrets. Port of infra/modules/keyvault/main.bicep.
#
# The vault holds NO secret values in code and never will: a secret value in a
# committed file lives forever in the forge's git history. The vault is created
# empty; values are written out of band by an operator. Terraform manages the
# vault, RBAC, and secret NAMES/REFERENCES only — never secret material.

variable "location" {
  description = "Deployment region. Key Vault region is fixed per vault and cannot be moved."
  type        = string
}

variable "tenant_id" {
  description = "Directory (tenant) id that owns the vault."
  type        = string
}

variable "environment_name" {
  description = "Short environment name for the shared tag set."
  type        = string
}

variable "key_vault_name" {
  description = "Name of the vault. Must be globally unique."
  type        = string
}

variable "owner_contact" {
  description = "Owner contact recorded on the vault."
  type        = string
}

variable "enable_private_network" {
  description = "Enables private-endpoint-only access (V5). When false the vault keeps a public endpoint and network ACLs stay wide open, because a Deny default with no private endpoint would lock the workload out of its own secrets."
  type        = bool
  default     = false
}

variable "soft_delete_retention_in_days" {
  description = "Days a soft-deleted secret is recoverable for. 90 is the platform maximum."
  type        = number
  default     = 90
}

variable "resource_group_name" {
  description = "Name of the identity resource group the vault is created in."
  type        = string
}

variable "tags" {
  description = "Shared tag set from the root module."
  type        = map(string)
}
