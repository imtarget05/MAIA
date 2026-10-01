// MAIA V1 parameters — DEV. Security foundation only (identity + vault +
// RBAC). No secret values, ever. Operator overrides before any real
// deployment: ownerContact, keyVaultName (globally unique),
// entraApplicationName (tenant-unique), deployIdentityPrincipalId (the zero
// GUID below fails what-if loudly on purpose).
using '../main.bicep'

param environmentName = 'dev'
param location = 'eastasia'
param tenantId = '00000000-0000-0000-0000-000000000000'
param ownerContact = 'platform-team@example.invalid'
param identityResourceGroupName = 'rg-maia-dev-identity'
param edgeResourceGroupName = 'rg-maia-dev-edge'
param appsResourceGroupName = 'rg-maia-dev-apps'
param managedIdentityName = 'id-maia-dev'
param entraApplicationName = 'maia-dev'
param webRedirectUris = []
param spaRedirectUris = []
param keyVaultName = 'kv-maia-dev-01'
param enablePrivateNetworkForVault = false
param softDeleteRetentionInDays = 90
param deployIdentityPrincipalId = '00000000-0000-0000-0000-000000000000'
