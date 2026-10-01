// MAIA V1 parameters — PROD. Same contract as v1-dev: no secret values,
// operator-owned unique names, zero-GUID deploy principal until supplied.
using '../main.bicep'

param environmentName = 'prod'
param location = 'eastasia'
param tenantId = '00000000-0000-0000-0000-000000000000'
param ownerContact = 'platform-team@example.invalid'
param identityResourceGroupName = 'rg-maia-identity'
param managedIdentityName = 'id-maia'
param entraApplicationName = 'maia'
param webRedirectUris = []
param spaRedirectUris = []
param keyVaultName = 'kv-maia-01'
param enablePrivateNetworkForVault = false
param deployIdentityPrincipalId = '00000000-0000-0000-0000-000000000000'
