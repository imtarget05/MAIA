// MAIA on Azure — V1 ONLY entrypoint: security foundation.
//
// Scope contract (enforced by infra/scripts/validate-v1-scope.py, gated in
// CI): this file may deploy Managed Identity, Key Vault and RBAC — nothing
// else. Edge (Front Door), APIM, ACR, observability and all later-wave
// resources live in main.v6-target.bicep. Splitting entrypoints (not feature
// flags) so a V1 deployment cannot accidentally provision a later-wave
// resource: there is no parameter combination that adds one.
//
// The three resource groups are all created here: groups are free, and the
// identity/edge/apps boundary is what scopes the deploy identity's
// Contributor grant (see infra/modules/rbac). Creating the empty edge/apps
// groups now does not create any billable resource inside them.

targetScope = 'subscription'

@description('Short environment name, e.g. "prod" or "dev". Drives the shared tag set and the default resource-name suffixes.')
param environmentName string

@description('Primary region for the whole stack. One region end to end.')
param location string

@description('Directory (tenant) id that owns the Key Vault and the token issuer.')
param tenantId string

@description('Owner contact recorded on every resource group and resource, e.g. a monitored mailbox.')
param ownerContact string

@description('Name of the resource group holding the managed identity, the app registration and Key Vault.')
param identityResourceGroupName string

@description('Name of the resource group reserved for Front Door Premium, its WAF policy and API Management. Created empty in V1; edge resources land here only via main.v6-target.bicep.')
param edgeResourceGroupName string

@description('Name of the resource group reserved for ACR, the Container Apps environment, the container app and observability. Created empty in V1; workloads land here only via main.v6-target.bicep.')
param appsResourceGroupName string

@description('Name of the user-assigned managed identity attached to the MAIA container app.')
param managedIdentityName string

@description('Tenant-unique name of the Entra application registration. Must contain no spaces.')
param entraApplicationName string

@description('Redirect URIs registered on the Entra `web` platform. Empty until a gateway URL exists to register.')
param webRedirectUris array

@description('Redirect URIs registered on the Entra `spa` platform (public client, PKCE). Empty until needed.')
param spaRedirectUris array

@description('Globally unique Key Vault name.')
param keyVaultName string

@description('True when Key Vault is reachable only through a private endpoint. V5 switch; false in V1.')
param enablePrivateNetworkForVault bool = false

@description('Days a soft-deleted secret is recoverable for. 90 is the platform maximum.')
param softDeleteRetentionInDays int = 90

@description('Object id of the identity GitHub Actions deploys as. Created out of band; this deployment only grants it Contributor on the identity group below.')
param deployIdentityPrincipalId string

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
  // Same scoping constraint as above.
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
    softDeleteRetentionInDays: softDeleteRetentionInDays
  }
}

// The ONLY RBAC invocation in V1: Key Vault Secrets User for the app identity
// plus deploy-identity Contributor scoped to the identity group. Edge/apps
// groups get their grants only in main.v6-target.bicep, when workloads exist
// there to justify them.
module rbacIdentity './modules/rbac/main.bicep' = {
  name: 'maia-rbac-identity'
  // Same scoping constraint as above.
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

@description('URI of the Key Vault holding the runtime secrets.')
output maiaKeyVaultUri string = keyVault.outputs.keyVaultUri

@description('Names of the Key Vault secrets the container app will resolve. Values are provisioned out of band; see docs/deployment-azure.md §5.')
output maiaKeyVaultSecretNames array = []

@description('Client id of the container app managed identity; pass as AZURE_CLIENT_ID for out-of-band data-plane calls.')
output maiaManagedIdentityClientId string = identity.outputs.managedIdentityClientId

@description('Application (client) id of the Entra registration.')
output maiaEntraApplicationClientId string = identity.outputs.entraApplicationClientId

@description('Resource groups this deployment owns. A destroy runbook that lists anything else as safe to delete is wrong.')
output maiaResourceGroupNames array = [
  identityResourceGroupName
  edgeResourceGroupName
  appsResourceGroupName
]
