# MAIA dev environment values. Same contract as prod: placeholders only,
# operator-owned unique names, zero-GUID placeholders until supplied.

environment_name                 = "dev"
location                         = "eastasia"
tenant_id                        = "00000000-0000-0000-0000-000000000000"
owner_contact                    = "platform-team@example.invalid"
identity_resource_group_name     = "rg-maia-identity-dev"
managed_identity_name            = "id-maia-dev"
entra_application_name           = "maia-dev"
web_redirect_uris                = []
spa_redirect_uris                = []
key_vault_name                   = "kv-maia-dev-01"
enable_private_network_for_vault = false
deploy_identity_principal_id     = "00000000-0000-0000-0000-000000000000"
soft_delete_retention_in_days    = 90
