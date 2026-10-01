mock_provider "azurerm" {}
mock_provider "azuread" {}

# Composition contract for the V1 deployment unit (TF-M13). The module-level
# tests cover each module in isolation; these cover the ROOT wiring that none of
# them can see: that the identity resource group is created, that the three
# modules are actually instantiated, and that the tag/role-id contract from
# locals.tf reaches them.
#
# WHY the assertions only touch plan-known values: a module OUTPUT derived from
# a computed attribute (managed_identity_client_id, key_vault_uri, ...) is
# unknown at plan time, and asserting on it fails with
#   Error: Unknown condition value
# even though the wiring is correct. So this file asserts root resources and
# the outputs that derive from literals. Per-module computed attributes are
# covered by that module's own tests/contract.tftest.hcl, which runs against the
# same mocked providers.

run "identity_group_is_created_with_the_tag_contract" {
  command = plan

  variables {
    environment_name             = "prod"
    location                     = "eastasia"
    tenant_id                    = "00000000-0000-0000-0000-000000000000"
    owner_contact                = "platform-team@example.invalid"
    identity_resource_group_name = "rg-maia-identity"
    managed_identity_name        = "id-maia"
    entra_application_name       = "maia"
    key_vault_name               = "kv-maia-01"
    deploy_identity_principal_id = "00000000-0000-0000-0000-000000000000"
  }

  assert {
    condition     = azurerm_resource_group.identity.name == "rg-maia-identity"
    error_message = "The identity resource group name drifted from the V1 contract."
  }

  assert {
    condition     = azurerm_resource_group.identity.location == "eastasia"
    error_message = "The identity resource group is not in the requested region."
  }

  assert {
    condition     = azurerm_resource_group.identity.tags["managedBy"] == "terraform"
    error_message = "managedBy tag drifted: a Terraform-managed group still claiming 'bicep' misleads the compliance query."
  }

  assert {
    condition     = azurerm_resource_group.identity.tags["project"] == "maia"
    error_message = "project tag drifted from the shared tag set."
  }
}

run "rbac_role_ids_are_wired_from_root_locals" {
  command = plan

  variables {
    environment_name             = "prod"
    location                     = "eastasia"
    tenant_id                    = "00000000-0000-0000-0000-000000000000"
    owner_contact                = "platform-team@example.invalid"
    identity_resource_group_name = "rg-maia-identity"
    managed_identity_name        = "id-maia"
    entra_application_name       = "maia"
    key_vault_name               = "kv-maia-01"
    deploy_identity_principal_id = "00000000-0000-0000-0000-000000000000"
  }

  # assigned_role_definition_ids is a list of literals passed down from
  # locals.tf, so it IS known at plan: this proves the root actually forwards
  # the four role ids instead of the module falling back to its own.
  assert {
    condition     = length(module.rbac.assigned_role_definition_ids) == 4
    error_message = "The rbac module did not receive the four role definition ids from locals.tf."
  }

  assert {
    condition     = module.rbac.assigned_role_definition_ids[0] == "/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6"
    error_message = "Key Vault Secrets User role id drifted at the root wiring (privilege escalation risk if replaced)."
  }

  assert {
    condition     = module.rbac.assigned_role_definition_ids[3] == "/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c"
    error_message = "Contributor role id drifted at the root wiring."
  }
}
