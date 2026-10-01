resource "azurerm_key_vault" "vault" {
  name                = var.key_vault_name
  location            = var.location
  resource_group_name = var.resource_group_name
  tenant_id           = var.tenant_id
  sku_name            = "standard"

  # RBAC only (renamed upstream in azurerm v4; same semantics as the old name).
  # The access-policy model is a second authorisation system that is
  # off-by-default in a new vault; leaving it enabled next to RBAC is how
  # a "no access" audit finding happens.
  rbac_authorization_enabled = true

  enabled_for_deployment          = false
  enabled_for_disk_encryption     = false
  enabled_for_template_deployment = false

  # Soft delete is always on in azurerm v4 (no disable switch); the retention
  # window is still configurable, and 90 days is what Bicep pins explicitly.
  soft_delete_retention_days = var.soft_delete_retention_in_days

  # Purge protection turns an accidental secret delete from an outage into a
  # support ticket. Irreversible once enabled. This is the property the plan
  # invariant checker protects. MUST NOT be weakened without a signed reason.
  purge_protection_enabled = true

  # V5 switch: flipping enable_private_network to true closes the public path
  # (the single switch). Until then the endpoint stays open, because a Deny
  # default with no private endpoint would lock the workload out of its own secrets.
  public_network_access_enabled = !var.enable_private_network

  network_acls {
    bypass         = "AzureServices"
    default_action = var.enable_private_network ? "Deny" : "Allow"
  }

  tags = var.tags
}
