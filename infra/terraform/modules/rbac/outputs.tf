output "assigned_role_definition_ids" {
  description = "Role definition ids this module can assign, so the runbook can print them without re-deriving them from code."
  value = [
    var.key_vault_secrets_user_role_id,
    var.acr_pull_role_id,
    var.storage_blob_data_role_id,
    var.contributor_role_id,
  ]
}

output "scoped_resource_group_name" {
  description = "Scope these assignments were created in (resource-group id). Reserved for parity with infra/modules/rbac output."
  value       = var.scope_id
}
