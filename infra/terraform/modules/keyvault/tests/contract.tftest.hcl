# Source-parity contract for the Key Vault module (TF-M11, mocked providers).
# purge protection is the property the plan invariant checker also protects.

mock_provider "azurerm" {}

run "vault_security_posture" {
  command = plan

  variables {
    resource_group_name           = "rg-maia-identity"
    location                      = "eastasia"
    tenant_id                     = "00000000-0000-0000-0000-000000000000"
    environment_name              = "prod"
    key_vault_name                = "kv-maia-01"
    owner_contact                 = "platform-team@example.invalid"
    enable_private_network        = false
    soft_delete_retention_in_days = 90
    tags = {
      env       = "prod"
      project   = "maia"
      managedBy = "terraform"
      owner     = "platform-team@example.invalid"
    }
  }

  assert {
    condition     = azurerm_key_vault.vault.purge_protection_enabled == true
    error_message = "Purge protection lost: an accidental secret delete becomes an outage, not a ticket."
  }

  assert {
    condition     = azurerm_key_vault.vault.rbac_authorization_enabled == true
    error_message = "Vault drifted off RBAC-only authorisation."
  }

  assert {
    condition     = azurerm_key_vault.vault.soft_delete_retention_days == 90
    error_message = "Soft-delete retention drifted from the pinned 90-day platform maximum."
  }

  assert {
    condition     = azurerm_key_vault.vault.public_network_access_enabled == true
    error_message = "V1 parity expects the public endpoint OPEN (private switch arrives in V5)."
  }
}
