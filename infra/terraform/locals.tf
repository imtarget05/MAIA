# Shared locals: the tag contract and the well-known RBAC role definition ids.
#
# PARITY NOTE (managedBy): Bicep tags every resource managedBy='bicep'. A
# Terraform-managed resource whose tag still says 'bicep' is a lie that will
# confuse the compliance query ("what is in MAIA's footprint") and the destroy
# runbook. During migration the value switches to 'terraform'. On Phase 3
# import this is the single known tag-level drift and is EXPECTED, not a bug.

locals {
  tags = {
    env       = var.environment_name
    project   = "maia"
    managedBy = "terraform"
    owner     = var.owner_contact
  }

  # Well-known role definition ids (tenant-level paths, no subscription needed).
  # Same GUIDs as infra/modules/rbac/main.bicep `subscriptionResourceId(...)`.
  key_vault_secrets_user_role_id = "/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6"
  acr_pull_role_id               = "/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
  storage_blob_data_role_id      = "/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
  # Contributor is granted on the identity SCOPE (azurerm_resource_group.identity
  # in main.tf), never the subscription. A subscription-scoped Contributor could
  # attach its own role assignments and escalate to Owner; group-scoped it cannot.
  contributor_role_id = "/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c"
}
