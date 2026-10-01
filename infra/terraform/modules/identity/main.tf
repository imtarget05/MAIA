resource "azurerm_user_assigned_identity" "workload" {
  name                = var.managed_identity_name
  location            = var.location
  resource_group_name = var.resource_group_name

  tags = var.tags
}

# Entra application registration: machine identity for the enterprise Azure
# deployment, managed-identity only, no interactive sign-in.
# Parity with the Bicep Graph resource:
#  - sign_in_audience AzureADMyOrg
#  - access_token_issuance disabled for web (no implicit flow, v2 tokens)
#  - allowed_member_types EMPTY on both platforms ("no user may sign in")
#  - NO required_resource_access / pre-consent (unused standing privilege)
resource "azuread_application_registration" "workload" {
  display_name = var.entra_application_name
  description  = "MAIA API — machine identity for the enterprise Azure deployment. Managed identity only; no interactive sign-in."

  sign_in_audience = "AzureADMyOrg"

  # requestedAccessTokenVersion: 2 (required by the APIM validate-jwt policy).
  requested_access_token_version = 2
}

resource "azuread_service_principal" "workload" {
  client_id = azuread_application_registration.workload.client_id
}

# `spa` platform: public client, PKCE only, redirect URIs only.
resource "azuread_application_redirect_uris" "spa" {
  application_id = azuread_application_registration.workload.id
  type           = "SPA"
  redirect_uris  = var.spa_redirect_uris
}

# `web` platform: redirect URIs only, implicit grant disabled on both tokens
# (deprecated flow, incompatible with v2 tokens).
resource "azuread_application_redirect_uris" "web" {
  application_id = azuread_application_registration.workload.id
  type           = "Web"
  redirect_uris  = var.web_redirect_uris
}
