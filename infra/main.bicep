// MAIA on Azure — V1. The deployment unit for this wave: identity, Key Vault
// and RBAC. Nothing else.
//
// WHY V1 is not main.v6-target.bicep: that file is the full end-state wiring
// (APIM, Front Door, edge, apps, observability), and deploying it from V1 would
// create five Azure services — real monthly cost and a real WAF/CDN attack
// surface — that no V1 acceptance criterion asks for. Those modules stay on
// disk, unreferenced, and get wired in at the wave that needs them. Two
// entrypoints, one shared set of modules: identity, keyvault and rbac are
// byte-identical in both waves, and a second copy of a security boundary is a
// second thing that has to be reviewed.
//
// NO DEPLOYMENT HAS HAPPENED. This file is validated by `infra/validate.sh`
// (compile + linter + negative tests) and by the `iac-validate` CI job, neither
// of which authenticates to Azure. The first real `az deployment sub create`
// is a separate, explicitly-authorised step.
//
// Deployment order is not arbitrary. The identity has to exist before RBAC can
// name its principal; RBAC runs last so that a partial failure leaves a stack
// with no privileges rather than a privileged stack with no identity.
//
// This file holds parameters and wiring only. Every resource decision lives in
// infra/modules/<concern>/, with the reason it is that way recorded next to it.

targetScope = 'subscription'

@description('Short environment name, e.g. "prod" or "dev". Drives the shared tag set.')
param environmentName string

@description('Primary region for the V1 resources. The managed identity must sit in the same region as the workload that authenticates with it, and a Key Vault region is fixed at creation.')
param location string

@description('Directory (tenant) id that owns the Key Vault and the app registration.')
param tenantId string

@description('Owner contact recorded in the tag set of every V1 resource. Must be a monitored mailbox: it is the only human route back to these resources.')
param ownerContact string

@description('Name of the resource group holding the managed identity, the Entra app registration and Key Vault. V1 creates this group only; the edge and apps groups arrive with the waves that use them.')
param identityResourceGroupName string

@description('Name of the user-assigned managed identity the MAIA workload runs as. Referenced from the container app, from APIM federated trust and from GitHub OIDC, which is why it is user-assigned rather than system-assigned.')
param managedIdentityName string

@description('Tenant-unique name of the Entra application registration. Must contain no spaces.')
param entraApplicationName string

@description('Redirect URIs on the Entra `web` platform. Empty in V1: the APIM gateway and the Container Apps FQDN these point at are not deployed yet, and registering a URI nothing serves is a dangling trust.')
param webRedirectUris array = []

@description('Redirect URIs on the Entra `spa` platform (public client, PKCE only). Empty in V1 for the same reason.')
param spaRedirectUris array = []

@description('Globally unique Key Vault name. 3-24 alphanumeric and hyphen characters, cannot end in a hyphen.')
param keyVaultName string

@description('True when Key Vault is reachable only through a private endpoint. V5 switch; false in V1, because a Deny default with no private endpoint locks the workload out of its own secrets.')
param enablePrivateNetworkForVault bool = false

@description('Object id of the identity GitHub Actions deploys as. This deployment grants it Contributor on the V1 resource group and nothing else. Registered out of band by an operator; no script and no value is committed here.')
param deployIdentityPrincipalId string


// WHY the resource group is declared here and not via resourceGroups.bicep:
// that module creates all three groups, and two of them (edge, apps) have no
// member in V1. Creating empty groups to be filled in by a later wave is not
// free — each one widens the blast radius the deploy identity can destroy.
resource identityResourceGroup 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: identityResourceGroupName
  location: location
  tags: {
    env: environmentName
    project: 'maia'
    managedBy: 'bicep'
    owner: ownerContact
  }
}

module identity './modules/identity/main.bicep' = {
  name: 'maia-identity'
  // Scoped by name, not by a module output: a module scope has to be computable
  // before the deployment starts, and resourceGroup(<id>) is not. The dependsOn
  // is what orders group creation before the group-scoped module.
  scope: resourceGroup(subscription().subscriptionId, identityResourceGroupName)
  dependsOn: [
    identityResourceGroup
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
  scope: resourceGroup(subscription().subscriptionId, identityResourceGroupName)
  dependsOn: [
    identityResourceGroup
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

module rbacIdentity './modules/rbac/main.bicep' = {
  name: 'maia-rbac-identity'
  scope: resourceGroup(subscription().subscriptionId, identityResourceGroupName)
  dependsOn: [
    identityResourceGroup
  ]
  // No explicit dependsOn on the identity module: Bicep infers it from
  // identity.outputs.managedIdentityPrincipalId below, which is the real
  // dependency. Naming it again here is a linter warning and no stronger.
  params: {
    appManagedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    deployIdentityPrincipalId: deployIdentityPrincipalId
    keyVaultName: keyVaultName
  }
}

@description('Resource id of the user-assigned managed identity. Bound by the container app at the wave that deploys it.')
output maiaManagedIdentityId string = identity.outputs.managedIdentityId

@description('Client id of the managed identity. Passed as AZURE_CLIENT_ID for out-of-band data-plane calls.')
output maiaManagedIdentityClientId string = identity.outputs.managedIdentityClientId

@description('Object id of the managed identity service principal. The value every runtime role assignment targets — never the client id.')
output maiaManagedIdentityPrincipalId string = identity.outputs.managedIdentityPrincipalId

@description('Application (client) id of the Entra registration. The `aud` claim the APIM validate-jwt policy checks against once the gateway is deployed.')
output maiaEntraApplicationClientId string = identity.outputs.entraApplicationClientId

@description('Object id (GUID) of the Entra application. What `az ad sp show --id` expects.')
output maiaEntraApplicationObjectId string = identity.outputs.entraApplicationObjectId

@description('URI of the Key Vault holding the runtime secrets.')
output maiaKeyVaultUri string = keyVault.outputs.keyVaultUri

@description('Role definition ids granted by this deployment, so a reviewer can diff them with `az role assignment list` instead of reading the template.')
output maiaAssignedRoleDefinitionIds array = rbacIdentity.outputs.assignedRoleDefinitionIds

@description('Resource groups this deployment owns. A destroy runbook that lists anything else as safe to delete is wrong.')
output maiaResourceGroupNames array = [
  identityResourceGroupName
]
