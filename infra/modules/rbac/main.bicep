// Role assignments for one resource group of the MAIA stack, all least-privilege
// and all scoped to a single named resource inside that group.
//
// WHY the module is resource-group scoped and is invoked once per group from
// infra/main.bicep: an extension resource cannot change scope, and the stack
// is split across three groups. One module per group keeps every `scope` an
// explicit `resourceGroup()` or a named resource rather than an interpolated
// resource-id string, which is not a type Bicep can check.
//
// WHY the deploy identity gets Contributor on these resource GROUPS and not on
// the subscription: a subscription-scoped Contributor can attach its own role
// assignments and therefore escalate itself to Owner. Scoped to a resource
// group it cannot. The result is that the CI identity can roll back this stack
// and nothing else — a claim a reviewer can check with one
// `az role assignment list` instead of taking on trust.

targetScope = 'resourceGroup'

@description('Object id (principal id) of the user-assigned managed identity the MAIA container app runs as. The target of every runtime grant.')
param appManagedIdentityPrincipalId string

@description('Object id of the identity GitHub Actions deploys as, created by infra/scripts/register-oidc-federation.sh. The target of the Contributor grant.')
param deployIdentityPrincipalId string

@description('Name of the Key Vault in this resource group. Empty when the group holds no vault.')
param keyVaultName string = ''

@description('Name of the container registry in this resource group. Empty when the group holds no registry.')
param containerRegistryName string = ''

@description('Name of the blob storage account for the app identity. Empty in V1 because no account exists yet; the assignment is emitted only when this is set, so the template never binds a role to a resource that is not there.')
param blobStorageAccountName string = ''

// WHY the role definition ids are derived from subscriptionResourceId rather
// than written as bare GUIDs: the values are stable, but a bare GUID in a
// template is unreadable and a reader cannot tell which role it is.
var keyVaultSecretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var storageBlobDataContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var contributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = if (!empty(keyVaultName)) {
  name: keyVaultName
  scope: resourceGroup()
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = if (!empty(containerRegistryName)) {
  name: containerRegistryName
  scope: resourceGroup()
}

resource blobStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = if (!empty(blobStorageAccountName)) {
  name: blobStorageAccountName
  scope: resourceGroup()
}

// Key Vault Secrets User, not Secrets Officer: the app resolves its own
// secrets and must never be able to change them. A compromised container that
// can rotate JWT_SECRET_KEY can mint tokens for every user in the tenant.
resource keyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(keyVaultName)) {
  name: guid(keyVault.id, appManagedIdentityPrincipalId, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: keyVaultSecretsUserRoleId
    principalId: appManagedIdentityPrincipalId
    // Explicit because a role assignment against a service principal without
    // this field fails with a principal-not-found error that reads like a
    // replication problem rather than a typing problem.
    principalType: 'ServicePrincipal'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(containerRegistryName)) {
  name: guid(containerRegistry.id, appManagedIdentityPrincipalId, acrPullRoleId)
  scope: containerRegistry
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: appManagedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// V4 extension point: emitted only once a storage account name is supplied.
resource blobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(blobStorageAccountName)) {
  name: guid(blobStorage.id, appManagedIdentityPrincipalId, storageBlobDataContributorRoleId)
  scope: blobStorage
  properties: {
    roleDefinitionId: storageBlobDataContributorRoleId
    principalId: appManagedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource deployIdentityContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, deployIdentityPrincipalId, contributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: contributorRoleId
    principalId: deployIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Role definition ids this module can assign, so the runbook can print them without re-deriving them from the template.')
output assignedRoleDefinitionIds array = [
  keyVaultSecretsUserRoleId
  acrPullRoleId
  storageBlobDataContributorRoleId
  contributorRoleId
]

@description('Name of the resource group these assignments were created in, so a reviewer can diff the three invocations against the three groups.')
output scopedResourceGroupName string = resourceGroup().name
