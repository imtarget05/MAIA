// Resource group boundary for the MAIA enterprise stack.
//
// WHY three groups and not one: the blast radius of a compromised deploy
// identity must be provable from Azure role assignments alone. A Contributor
// scoped to exactly these three names can destroy the whole stack and nothing
// else; the same grant at subscription scope would reach rg-portfolio-evidence
// and the shared `cae-portfolio` environment that other services depend on.

targetScope = 'subscription'

@description('Deployment region. Every MAIA resource in this stack is regional, so one region is used end to end to keep Private Link and VNet (V5) paths simple.')
param location string

@description('Short environment name, e.g. "prod" or "dev". Propagated into the shared tag set so every resource is attributable.')
param environmentName string

@description('Name of the resource group holding the app managed identity, the Entra app registration and Key Vault.')
param identityResourceGroupName string

@description('Name of the resource group holding Front Door Premium, its WAF policy and API Management.')
param edgeResourceGroupName string

@description('Name of the resource group holding ACR, the Container Apps environment, the container app and Log Analytics/App Insights.')
param appsResourceGroupName string

@description('Owner contact recorded on every resource group, e.g. "platform-eng@contoso.com".')
param ownerContact string

// WHY one shared tag object instead of per-resource literals: a compliance
// query ("what is in MAIA's prod footprint") is a single tag filter, and it
// cannot drift because there is only one place to edit.
var tags = {
  env: environmentName
  project: 'maia'
  managedBy: 'bicep'
  owner: ownerContact
}

resource identityRg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: identityResourceGroupName
  location: location
  tags: tags
}

resource edgeRg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: edgeResourceGroupName
  location: location
  tags: tags
}

resource appsRg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: appsResourceGroupName
  location: location
  tags: tags
}

@description('Fully qualified resource ID of the identity resource group. Role assignments in infra/modules/rbac are scoped from these values, never from resourceGroup().')
output identityResourceGroupId string = identityRg.id

@description('Fully qualified resource ID of the edge resource group.')
output edgeResourceGroupId string = edgeRg.id

@description('Fully qualified resource ID of the apps resource group.')
output appsResourceGroupId string = appsRg.id
