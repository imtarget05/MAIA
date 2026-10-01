output "managed_identity_id" {
  description = "Resource id of the user-assigned managed identity."
  value       = azurerm_user_assigned_identity.workload.id
}

output "managed_identity_principal_id" {
  description = "Object id of the managed identity service principal. Target of every runtime role assignment — never the client id."
  value       = azurerm_user_assigned_identity.workload.principal_id
}

output "managed_identity_client_id" {
  description = "Client id of the managed identity. Passed to AZURE_CLIENT_ID."
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "entra_application_client_id" {
  description = "Application (client) id of the Entra registration. The `aud` claim APIM validate-jwt checks against."
  value       = azuread_application_registration.workload.client_id
}

output "entra_application_object_id" {
  description = "Object id (GUID) of the Entra application. What `az ad sp show --id` expects."
  value       = azuread_application_registration.workload.object_id
}

output "entra_service_principal_app_id" {
  description = "App id of the Entra service principal. Equal to the application client id."
  value       = azuread_service_principal.workload.client_id
}
