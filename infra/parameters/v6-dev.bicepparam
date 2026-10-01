// MAIA enterprise stack — DEV parameters (validation only in TODO 3).
//
// Nothing here is secret: secret VALUES are never committed (see
// infra/modules/keyvault/main.bicep). Names, regions and example contacts
// are safe to read in a PR. Before any real deployment the operator must
// replace: ownerContact, keyVaultName (globally unique), containerRegistryName
// (globally unique), entraApplicationName (tenant-unique), and
// deployIdentityPrincipalId (zero GUID below fails what-if loudly on purpose).
using '../main.v6-target.bicep'

param environmentName = 'dev'
param location = 'eastasia'
param tenantId = '00000000-0000-0000-0000-000000000000'
param entraLoginEndpoint = 'https://login.microsoftonline.com/'
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
param containerRegistryName = 'acrmaiadev01'
param containerAppsEnvironmentName = 'cae-maia-dev'
param containerAppName = 'ca-maia-dev'
param containerImage = 'ghcr.io/imtarget05/maia-maia-api:a82f24b2123b5abceeeb5264677aa81bbc437df7'
param revisionSuffix = 'v1-validate'
param keyVaultSecretNames = [
  'demo-user-a-pw'
  'demo-user-b-pw'
]
param plainEnvironmentVariables = [
  {
    name: 'ENVIRONMENT'
    value: 'development'
  }
]
param allowedOrigins = []
param frontDoorResourceNames = {
  profile: 'fd-maia-dev'
  endpoint: 'fde-maia-dev'
  originGroup: 'fdog-maia-dev'
  origin: 'fdo-maia-dev'
  route: 'fdr-maia-dev'
  wafPolicy: 'waf-maia-dev'
  securityPolicy: 'fdsec-maia-dev'
}
param apimResourceNames = {
  service: 'apim-maia-dev'
  backend: 'maia-backend'
  api: 'maia-api'
  apiPath: 'maia'
}
param apiOperations = []
param logAnalyticsWorkspaceName = 'log-maia-dev'
param applicationInsightsName = 'appi-maia-dev'
param deployIdentityPrincipalId = '00000000-0000-0000-0000-000000000000'
param blobStorageAccountName = ''
