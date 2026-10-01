// V1 parameters — SHAPE ONLY. Every value here is a placeholder.
//
// WHY placeholders: this file is committed, and a real tenantId, a real
// resource-group name or a real principal id is environment-specific data that
// would have to be edited before the first use and would then sit in git
// history forever. The real file is produced out of band by an operator and
// passed with `--parameters @<file>`; it is never committed.
//
// WHY the deploy identity principal is a placeholder GUID and not empty: an
// empty value would fail the parameter type check and teach the wrong lesson
// (that the field is optional). It is required, and a deployment without it
// would grant Contributor to nobody.
//
// HOW TO PRODUCE THE REAL FILE: copy this to a path outside the repo, replace
// every value, and confirm with `git check-ignore` that the real path is not
// tracked. Validate it with `bicep build-params` before the first deployment.

// WHY '../main.bicep' and not './main.bicep': a `using` path is relative to the
// .bicepparam file, not to the working directory. From infra/params/ the
// entrypoint one level up is the V1 deployment unit.
using '../main.bicep'

// Short environment tag, e.g. 'dev' or 'prod'.
param environmentName = 'dev'

// Azure region for the V1 resources. One region end to end.
param location = 'swedencentral'

// Directory (tenant) id. From `az account show --query tenantId -o tsv`.
param tenantId = '00000000-0000-0000-0000-000000000000'

// Monitored mailbox recorded in the tag set of every resource.
param ownerContact = 'platform-eng@example.invalid'

// Resource group for identity, Entra app and Key Vault.
param identityResourceGroupName = 'rg-maia-identity-dev'

// User-assigned managed identity the workload runs as.
param managedIdentityName = 'id-maia-app-dev'

// Entra application registration. Tenant-unique, no spaces.
param entraApplicationName = 'maia-api-dev'

// Empty in V1: the APIM gateway and Container Apps FQDN are not deployed yet.
param webRedirectUris = [
]

param spaRedirectUris = [
]

// Globally unique. 3-24 alphanumeric and hyphen characters, no trailing hyphen.
param keyVaultName = 'kv-maia-dev-placeholder'

// False in V1. A Deny default with no private endpoint would lock the workload
// out of its own secrets.
param enablePrivateNetworkForVault = false

// Object id of the GitHub Actions deploy identity, registered out of band.
// From `az ad sp show --id <appId> --query id -o tsv` on the federated SP.
param deployIdentityPrincipalId = '00000000-0000-0000-0000-000000000000'
