# Workload identity: user-assigned managed identity + Entra app registration.
# Port of infra/modules/identity/main.bicep.
#
# WHY user-assigned: referenced from the container app, APIM federated trust
# and future GitHub OIDC federation; must survive destruction of whatever
# created it. WHY no secrets anywhere: managed identity removes the secret
# entirely — nothing to leak, rotate or expire.
#
# Terraform caveat (Graph): azuread resources live outside the resource group.
# The Entra application + service principal are tenant-scoped; only the UAMI
# is inside the group. This matches Bicep (identity module is group-scoped but
# Graph resources declare their own scope).

variable "resource_group_name" {
  description = "Name of the identity resource group the UAMI is created in."
  type        = string
}

variable "location" {
  description = "Deployment region; the managed identity must sit in the same region as the container app it authenticates for."
  type        = string
}

variable "environment_name" {
  description = "Short environment name for the shared tag set."
  type        = string
}

variable "managed_identity_name" {
  description = "Name of the user-assigned managed identity attached to the MAIA container app."
  type        = string
}

variable "entra_application_name" {
  description = "Unique name of the Entra application registration. Must be unique tenant-wide."
  type        = string
}

variable "web_redirect_uris" {
  description = "Redirect URIs on the `web` platform. Empty in V1."
  type        = list(string)
  default     = []
}

variable "spa_redirect_uris" {
  description = "Redirect URIs on the `spa` platform (public client, PKCE only). Empty in V1."
  type        = list(string)
  default     = []
}

variable "owner_contact" {
  description = "Owner contact recorded on the identity resource."
  type        = string
}

variable "tags" {
  description = "Shared tag set from the root module."
  type        = map(string)
}
