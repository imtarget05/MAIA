// NEGATIVE TEST: a type violation on a security-critical parameter.
//
// WHY this must fail: `deployIdentityPrincipalId` is fed straight into
// `principalId` on a Contributor role assignment. A number where a GUID object
// id belongs is the kind of mistake that either fails at the provider or, if
// the type were loosened to `any`, produces a role assignment with a garbage
// principal. The type system is the guard here, so this fixture proves the
// guard is still in place.
//
// If this file ever compiles, the parameter type was weakened. Hard stop.

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

// Wrong type on purpose: an int where a string object id is required.
param deployIdentityPrincipalId = 12345
