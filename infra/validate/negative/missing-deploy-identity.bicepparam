// NEGATIVE TEST: a required parameter is missing.
//
// WHY this must fail: `deployIdentityPrincipalId` is the principal that gets
// Contributor on the resource group. If it were optional, a deployment with no
// value would either fail at the ARM provider or — worse, if a default were
// added later — grant Contributor to a principal nobody chose. A missing
// required parameter is the cheapest possible failure and it has to stay that
// way, so this fixture is expected to FAIL `bicep build-params`.
//
// If this file ever compiles, someone made a security-critical parameter
// optional. That is a hard stop, not a warning.

using '../../main.bicep'

param environmentName = 'dev'
param location = 'swedencentral'
param tenantId = '00000000-0000-0000-0000-000000000000'
param ownerContact = 'platform-eng@example.invalid'
param identityResourceGroupName = 'rg-maia-identity-dev'
param managedIdentityName = 'id-maia-app-dev'
param entraApplicationName = 'maia-api-dev'
param webRedirectUris = [
]
param spaRedirectUris = [
]
param keyVaultName = 'kv-maia-dev-placeholder'
param enablePrivateNetworkForVault = false

// deployIdentityPrincipalId is deliberately absent. See above.
