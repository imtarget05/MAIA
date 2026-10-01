# Source-parity contract for the identity module (TF-M11). These run against
# mocked providers only; a PASS means "the module declares what V1 Bicep
# declares", not "an identity exists in Azure".

mock_provider "azurerm" {}
mock_provider "azuread" {}

run "uami_is_user_assigned_and_named" {
  command = plan

  variables {
    resource_group_name    = "rg-maia-identity"
    location               = "eastasia"
    environment_name       = "prod"
    managed_identity_name  = "id-maia"
    entra_application_name = "maia"
    owner_contact          = "platform-team@example.invalid"
    tags = {
      env       = "prod"
      project   = "maia"
      managedBy = "terraform"
      owner     = "platform-team@example.invalid"
    }
  }

  assert {
    condition     = azurerm_user_assigned_identity.workload.name == "id-maia"
    error_message = "UAMI name drifted from the V1 contract."
  }
}

run "entra_app_is_v2_no_implicit_no_consent" {
  command = plan

  variables {
    resource_group_name    = "rg-maia-identity"
    location               = "eastasia"
    environment_name       = "prod"
    managed_identity_name  = "id-maia"
    entra_application_name = "maia"
    owner_contact          = "platform-team@example.invalid"
    tags = {
      env       = "prod"
      project   = "maia"
      managedBy = "terraform"
      owner     = "platform-team@example.invalid"
    }
  }

  assert {
    condition     = azuread_application_registration.workload.sign_in_audience == "AzureADMyOrg"
    error_message = "Entra app left the AzureADMyOrg audience boundary."
  }

  assert {
    condition     = azuread_application_registration.workload.requested_access_token_version == 2
    error_message = "Entra app lost requestedAccessTokenVersion 2 (APIM validate-jwt requirement)."
  }
}
