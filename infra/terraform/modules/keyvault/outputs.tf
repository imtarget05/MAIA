output "key_vault_uri" {
  description = "URI of the vault, including the trailing slash. Used to build the keyVaultUrl of each Container Apps secret reference."
  value       = azurerm_key_vault.vault.vault_uri
}

output "key_vault_id" {
  description = "Resource id of the vault. The scope for the Key Vault Secrets User role assignment in modules/rbac."
  value       = azurerm_key_vault.vault.id
}

output "key_vault_name" {
  description = "Name of the vault, for `az keyvault secret set --vault-name` calls in the runbook."
  value       = azurerm_key_vault.vault.name
}
