// MAIA enterprise stack — PROD parameters (validation only in TODO 3).
//
// Same contract as dev.bicepparam: no secret values, ever. The operator must
// replace ownerContact, the three globally/tenant-unique names and
// deployIdentityPrincipalId before any real deployment. Prod differs from dev
// in names/tags only; no topology switch lives in this file.
using '../main.bicep'

param environmentName = 'prod'
param location = 'eastasia'
param tenantId = 'aa79a92c-ec09-4de1-baa9-151b8f9df886'
param entraLoginEndpoint = 'https://login.microsoftonline.com/'
param ownerContact = 'platform-team@example.invalid'
param identityResourceGroupName = 'rg-maia-identity'
param edgeResourceGroupName = 'rg-maia-edge'
param appsResourceGroupName = 'rg-maia-apps'
param managedIdentityName = 'id-maia'
param entraApplicationName = 'maia'
param webRedirectUris = []
param spaRedirectUris = []
param keyVaultName = 'kv-maia-01'
param enablePrivateNetworkForVault = false
param containerRegistryName = 'acrmaia01'
param containerAppsEnvironmentName = 'cae-maia'
param containerAppName = 'ca-maia'
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
  profile: 'fd-maia'
  endpoint: 'fde-maia'
  originGroup: 'fdog-maia'
  origin: 'fdo-maia'
  route: 'fdr-maia'
  wafPolicy: 'waf-maia'
  securityPolicy: 'fdsec-maia'
}
param apimResourceNames = {
  service: 'apim-maia'
  backend: 'maia-backend'
  api: 'maia-api'
  apiPath: 'maia'
}
param apiOperations = []
param logAnalyticsWorkspaceName = 'log-maia'
param applicationInsightsName = 'appi-maia'
param deployIdentityPrincipalId = '00000000-0000-0000-0000-000000000000'
param blobStorageAccountName = ''
