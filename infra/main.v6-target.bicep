// MAIA on Azure — V1 security foundation + V6 IaC/DevSecOps.
//
// Deployment order is not arbitrary. The identity has to exist before the
// container app can bind it; the container app has to exist before APIM and
// Front Door can be given a URL; APIM has to exist before its operations can
// be registered; RBAC runs last so that a partial failure leaves a stack with
// no privileges rather than a privileged stack with no workloads.
//
// This file holds parameters and wiring only. Every resource decision lives in
// infra/modules/<concern>/, with the reason it is that way recorded next to it.

targetScope = 'subscription'

@description('Short environment name, e.g. "prod" or "dev". Drives the shared tag set and the default resource-name suffixes.')
param environmentName string

@description('Primary region for the whole stack. One region end to end: Container Apps cannot pull from a cross-region registry over a private endpoint, and a cross-region APIM backend pays latency on every request.')
param location string

@description('Directory (tenant) id that owns the Key Vault, the app registration and the token issuer.')
param tenantId string

@description('Cloud login endpoint that validate-jwt fetches the tenant signing keys from, e.g. "https://login.microsoftonline.com/". No default: the value is cloud-specific, and a default pointing at the public cloud would silently validate against the wrong authority in a sovereign cloud.')
param entraLoginEndpoint string

@description('Email address recorded as the APIM publisher and in the resource tags. API Management requires a publisher email and uses it for service notifications, so it must be a monitored mailbox.')
param ownerContact string

@description('Name of the resource group holding the managed identity, the app registration and Key Vault.')
param identityResourceGroupName string

@description('Name of the resource group holding Front Door Premium, its WAF policy and API Management.')
param edgeResourceGroupName string

@description('Name of the resource group holding ACR, the Container Apps environment, the container app and the observability resources.')
param appsResourceGroupName string

@description('Name of the user-assigned managed identity attached to the MAIA container app.')
param managedIdentityName string

@description('Tenant-unique name of the Entra application registration. Must contain no spaces; it is the `aud` value APIM validates.')
param entraApplicationName string

@description('Redirect URIs on the Entra `web` platform: the APIM gateway first, the Container Apps FQDN second for direct debugging.')
param webRedirectUris array

@description('Redirect URIs on the Entra `spa` platform (public client, PKCE).')
param spaRedirectUris array

@description('Globally unique Key Vault name.')
param keyVaultName string

@description('True when Key Vault is reachable only through a private endpoint. V5 switch; false in V1.')
param enablePrivateNetworkForVault bool = false

@description('Globally unique container registry name.')
param containerRegistryName string

@description('Name of the Container Apps managed environment.')
param containerAppsEnvironmentName string

@description('Name of the MAIA container app.')
param containerAppName string

@description('Container image to run. Defaults to the GHCR image built by .github/workflows/build-container.yml. A tag, not a digest: a new revision re-resolves the tag, which is Container Apps\' equivalent of a pull policy of Always.')
param containerImage string

@description('Revision suffix. Bump it to force a new revision from an unchanged template, which is how a Key Vault secret change reaches a running app.')
param revisionSuffix string

@description('Key Vault secret names the app resolves. By contract each name IS the MAIA environment variable name, so one array produces both the secret reference and the env var that reads it. Must stay in step with src/maia/azure_identity.py KV_SECRET_NAMES.')
param keyVaultSecretNames array

@description('Non-secret environment variables for the app: ENVIRONMENT, CORS_ORIGINS, the managed identity coordinates, the OTLP endpoint. Anything secret belongs in keyVaultSecretNames instead.')
param plainEnvironmentVariables array

@description('Browser origins allowed to call the API. Applied both as CORS_ORIGINS on the app and as the allowed-origins list in the APIM policy; the two must not diverge.')
param allowedOrigins array

@description('Subnet delegated to Microsoft.App for the Container Apps environment. Empty in V1; supplying it moves the environment onto a VNet (V5).')
param infrastructureSubnetResourceId string = ''

@description('Zone redundancy of the Container Apps environment. Not supported on the consumption plan, so it stays false in V1.')
param zoneRedundantEnvironment bool = false

@description('Name of the Front Door profile, endpoint, origin group, origin, route, WAF policy and security policy.')
param frontDoorResourceNames object

@description('Name of the APIM service and of the backend, API and API path it exposes.')
param apimResourceNames object

@description('APIM SKU. "Consumption" for V1. "Developer" for staging. "Premium" is required for the V5 private-injection path.')
param apimSkuName string = 'Consumption'

@description('APIM network configuration. "None" on Consumption; "External" once a Premium instance is injected into a VNet in V5.')
param apimVirtualNetworkType string = 'None'

@description('Operations exposed at the gateway, with per-operation rate limits and whether each requires an Entra token. A MAIA route absent from this list returns 404 at the gateway — /metrics is deliberately absent.')
param apiOperations array

@description('Name of the Log Analytics workspace receiving every diagnostic stream in this stack.')
param logAnalyticsWorkspaceName string

@description('Name of the Application Insights component.')
param applicationInsightsName string

@description('Object id of the identity GitHub Actions deploys as. Created by infra/scripts/register-oidc-federation.sh; this deployment only grants it Contributor on the three new resource groups.')
param deployIdentityPrincipalId string

@description('Blob storage account for the app identity. Empty in V1; the Storage Blob Data Contributor assignment is emitted only when a name is supplied.')
param blobStorageAccountName string = ''

module resourceGroups './resourceGroups.bicep' = {
  name: 'maia-resource-groups'
  params: {
    location: location
    environmentName: environmentName
    identityResourceGroupName: identityResourceGroupName
    edgeResourceGroupName: edgeResourceGroupName
    appsResourceGroupName: appsResourceGroupName
    ownerContact: ownerContact
  }
}

module identity './modules/identity/main.bicep' = {
  name: 'maia-identity'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, identityResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    location: location
    environmentName: environmentName
    managedIdentityName: managedIdentityName
    entraApplicationName: entraApplicationName
    webRedirectUris: webRedirectUris
    spaRedirectUris: spaRedirectUris
    ownerContact: ownerContact
  }
}

module keyVault './modules/keyvault/main.bicep' = {
  name: 'maia-keyvault'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, identityResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    location: location
    tenantId: tenantId
    environmentName: environmentName
    keyVaultName: keyVaultName
    ownerContact: ownerContact
    enablePrivateNetwork: enablePrivateNetworkForVault
  }
}

module observability './modules/observability/main.bicep' = {
  name: 'maia-observability'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, appsResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    location: location
    environmentName: environmentName
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    applicationInsightsName: applicationInsightsName
    ownerContact: ownerContact
  }
}

module apps './modules/apps/main.bicep' = {
  name: 'maia-apps'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, appsResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    location: location
    environmentName: environmentName
    ownerContact: ownerContact
    containerRegistryName: containerRegistryName
    containerAppsEnvironmentName: containerAppsEnvironmentName
    zoneRedundantEnvironment: zoneRedundantEnvironment
    infrastructureSubnetResourceId: infrastructureSubnetResourceId
    containerAppName: containerAppName
    containerImage: containerImage
    revisionSuffix: revisionSuffix
    keyVaultUri: keyVault.outputs.keyVaultUri
    keyVaultSecretNames: keyVaultSecretNames
    plainEnvironmentVariables: plainEnvironmentVariables
    managedIdentityId: identity.outputs.managedIdentityId
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
  }
}

module apimService './modules/apim/service.bicep' = {
  name: 'maia-apim'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, edgeResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    location: location
    environmentName: environmentName
    ownerContact: ownerContact
    apimServiceName: apimResourceNames.service
    apimSkuName: apimSkuName
    apimPublisherEmail: ownerContact
    apimPublisherName: 'MAIA Platform'
    apimVirtualNetworkType: apimVirtualNetworkType
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
  }
}

module apimApiSurface './modules/apim/api-surface.bicep' = {
  name: 'maia-apim-api'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, edgeResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    apimServiceName: apimResourceNames.service
    backendName: apimResourceNames.backend
    apiName: apimResourceNames.api
    apiPath: apimResourceNames.apiPath
    apiDisplayName: 'MAIA API'
    apiDescription: 'MAIA retrieval-augmented generation assistant: /query, /chat, and the operational probes.'
    containerAppUrl: apps.outputs.containerAppUrl
    tenantId: tenantId
    entraApplicationClientId: identity.outputs.entraApplicationClientId
    entraLoginEndpoint: entraLoginEndpoint
    allowedOrigins: allowedOrigins
    apiOperations: apiOperations
  }
}

module edge './modules/edge/main.bicep' = {
  name: 'maia-edge'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, edgeResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    location: location
    environmentName: environmentName
    ownerContact: ownerContact
    frontDoorProfileName: frontDoorResourceNames.profile
    frontDoorEndpointName: frontDoorResourceNames.endpoint
    frontDoorOriginGroupName: frontDoorResourceNames.originGroup
    frontDoorOriginName: frontDoorResourceNames.origin
    frontDoorRouteName: frontDoorResourceNames.route
    frontDoorWafPolicyName: frontDoorResourceNames.wafPolicy
    frontDoorSecurityPolicyName: frontDoorResourceNames.securityPolicy
    containerAppFqdn: apps.outputs.containerAppFqdn
    containerAppsEnvironmentId: apps.outputs.containerAppsEnvironmentId
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
  }
}

module rbacIdentity './modules/rbac/main.bicep' = {
  name: 'maia-rbac-identity'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, identityResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    appManagedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    deployIdentityPrincipalId: deployIdentityPrincipalId
    keyVaultName: keyVaultName
  }
}

module rbacApps './modules/rbac/main.bicep' = {
  name: 'maia-rbac-apps'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, appsResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    appManagedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    deployIdentityPrincipalId: deployIdentityPrincipalId
    containerRegistryName: containerRegistryName
    blobStorageAccountName: blobStorageAccountName
  }
}

// The edge group holds no data-plane resources the app identity needs, so it
// gets only the deploy identity's Contributor grant.
module rbacEdge './modules/rbac/main.bicep' = {
  name: 'maia-rbac-edge'
  // Scoped by name, not by the resourceGroups module output: a module scope has to be
  // computable before the deployment starts, and resourceGroup(id) is not. The
  // dependsOn below is what orders the group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, edgeResourceGroupName)
  dependsOn: [
    resourceGroups
  ]
  params: {
    appManagedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    deployIdentityPrincipalId: deployIdentityPrincipalId
  }
}

@description('Public URL clients should be pointed at: the Front Door endpoint. The only MAIA URL intended for browsers.')
output maiaPublicUrl string = edge.outputs.frontDoorUrl

@description('Ingress URL of the container app. For in-cluster and debugging traffic only; it bypasses the WAF and the rate limits.')
output maiaContainerAppUrl string = apps.outputs.containerAppUrl

@description('APIM gateway URL of the MAIA API, used for direct API calls in the verification checklist and for the Entra redirect URI.')
output maiaApimApiUrl string = apimApiSurface.outputs.apiGatewayUrl

@description('APIM gateway hostname, which is one of the registered web redirect URIs on the Entra application.')
output maiaApimGatewayHostName string = apimService.outputs.apimGatewayHostName

@description('URI of the Key Vault holding the runtime secrets.')
output maiaKeyVaultUri string = keyVault.outputs.keyVaultUri

@description('Names of the Key Vault secrets the container app resolves. Values are provisioned out of band; see docs/deployment-azure.md §5.')
output maiaKeyVaultSecretNames array = keyVaultSecretNames

@description('Login server of the container registry.')
output maiaContainerRegistryLoginServer string = apps.outputs.containerRegistryLoginServer

@description('Client id of the Entra application registration, the audience APIM validate-jwt enforces.')
output maiaEntraApplicationClientId string = identity.outputs.entraApplicationClientId

@description('Object id of the Entra application registration.')
output maiaEntraApplicationObjectId string = identity.outputs.entraApplicationObjectId

@description('Client id of the container app managed identity; pass as AZURE_CLIENT_ID for out-of-band data-plane calls.')
output maiaManagedIdentityClientId string = identity.outputs.managedIdentityClientId

@description('Resource id of the Log Analytics workspace holding every diagnostic stream from this stack.')
output maiaLogAnalyticsWorkspaceId string = observability.outputs.logAnalyticsWorkspaceId

@description('Operations registered at the gateway. Anything not listed here is unreachable through APIM.')
output maiaApiOperations array = apimApiSurface.outputs.operationNames

@description('Resource groups this deployment owns. A destroy runbook that lists anything else as safe to delete is wrong.')
output maiaResourceGroupNames array = [
  identityResourceGroupName
  edgeResourceGroupName
  appsResourceGroupName
]
