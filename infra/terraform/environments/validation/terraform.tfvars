# MAIA validation environment values (transient enterprise validation per
# Phase 11: apply -> verify -> evidence -> destroy). Placeholders only.

environment_name                 = "validation"
location                         = "eastasia"
tenant_id                        = "00000000-0000-0000-0000-000000000000"
owner_contact                    = "platform-team@example.invalid"
identity_resource_group_name     = "rg-maia-identity-validation"
managed_identity_name            = "id-maia-validation"
entra_application_name           = "maia-validation"
web_redirect_uris                = []
spa_redirect_uris                = []
key_vault_name                   = "kv-maia-val-01"
enable_private_network_for_vault = false
deploy_identity_principal_id     = "00000000-0000-0000-0000-000000000000"
soft_delete_retention_in_days    = 90
