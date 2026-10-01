// MAIA V1 parameters — PROD. Same contract as v1-dev: no secret values,
// operator-owned unique names, zero-GUID deploy principal until supplied.
using '../main.bicep'

param environmentName = 'prod'
param location = 'eastasia'
// WHY a zero GUID and not the real tenant: a directory (tenant) id is not a
// credential and grants no access on its own, but it IS real
// environment-specific data that identifies one specific tenant indefinitely
// once pushed, and this repository has a public remote. The real value is
// supplied out of band with the rest of the parameter file. A zero GUID fails
// the ARM tenant lookup loudly at what-if, which is the intended behaviour for
// a placeholder: it cannot silently deploy against the wrong directory.
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
