# Outputs: parity with infra/main.bicep (V1), in the same order.

output "maia_managed_identity_id" {
  description = "Resource id of the user-assigned managed identity. Bound by the container app at the wave that deploys it."
  value       = module.identity.managed_identity_id
}

output "maia_managed_identity_client_id" {
  description = "Client id of the managed identity. Passed as AZURE_CLIENT_ID for out-of-band data-plane calls."
  value       = module.identity.managed_identity_client_id
}

output "maia_managed_identity_principal_id" {
  description = "Object id of the managed identity service principal. The value every runtime role assignment targets — never the client id."
  value       = module.identity.managed_identity_principal_id
}

output "maia_entra_application_client_id" {
  description = "Application (client) id of the Entra registration. The `aud` claim the APIM validate-jwt policy checks against once the gateway is deployed."
  value       = module.identity.entra_application_client_id
}

output "maia_entra_application_object_id" {
  description = "Object id (GUID) of the Entra application. What `az ad sp show --id` expects."
  value       = module.identity.entra_application_object_id
}

output "maia_key_vault_uri" {
  description = "URI of the Key Vault holding the runtime secrets."
  value       = module.keyvault.key_vault_uri
}

output "maia_assigned_role_definition_ids" {
  description = "Role definition ids granted by this deployment, so a reviewer can diff them with `az role assignment list`."
  value       = module.rbac.assigned_role_definition_ids
}

output "maia_resource_group_names" {
  description = "Resource groups this deployment owns. A destroy runbook that lists anything else as safe to delete is wrong."
  value       = [azurerm_resource_group.identity.name]
}
