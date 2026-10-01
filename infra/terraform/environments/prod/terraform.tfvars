# MAIA prod environment values. Mirrors infra/parameters/v1-prod.bicepparam:
# no secret VALUES, operator-owned unique names, zero-GUID placeholders until
# an operator/CI supplies the real ids via `-var` or backend config.
# Commit this file. Real secrets never go here — pass them at apply time.

environment_name                 = "prod"
location                         = "eastasia"
tenant_id                        = "00000000-0000-0000-0000-000000000000"
owner_contact                    = "platform-team@example.invalid"
identity_resource_group_name     = "rg-maia-identity"
managed_identity_name            = "id-maia"
entra_application_name           = "maia"
web_redirect_uris                = []
spa_redirect_uris                = []
key_vault_name                   = "kv-maia-01"
enable_private_network_for_vault = false
deploy_identity_principal_id     = "00000000-0000-0000-0000-000000000000"
soft_delete_retention_in_days    = 90
