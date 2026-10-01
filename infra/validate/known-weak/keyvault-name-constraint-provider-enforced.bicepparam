// CLOSED GAP — the Key Vault property check now exists.
//
// HISTORY, kept because the evidence trail matters more than a tidy file. On
// 2026-10-01 `enablePurgeProtection: true` was flipped to `false` in
// infra/modules/keyvault/main.bicep as an injected failure, and every check in
// infra/validate.sh still reported ALL CHECKS PASSED. Nothing inspected resource
// properties, so a security property could be turned off and the job stayed
// green. That was run, not assumed.
//
// THE FIX: infra/check_invariants.py compiles main.bicep to ARM JSON and asserts
// the V1 security invariants on that artifact. The same mutation was re-run
// afterwards and now fails the job with exit 1. A second property
// (enableRbacAuthorization) and a third case (the vault removed from the
// template entirely, which is reported as a failure rather than a skip) are
// covered the same way.
//
// A narrower gap remains and is NOT covered by an automated check:
// `param keyVaultName string` accepts any string, while the real constraint
// (3-24 alphanumeric and hyphen characters, no trailing hyphen) is enforced by
// the ARM provider at deployment time rather than at compile time. This file
// compiles, which is the proof that the constraint is still provider-enforced
// only. Fixing it means moving the constraint into a Bicep `assert` and enabling
// the Asserts experimental feature, which is a deliberate decision rather than a
// validator change.
//
// This fixture is therefore kept as the regression pin for the second gap only.
// Delete it when the name constraint is asserted in the template itself.

using '../../main.bicep'

// Identical to params/v1.dev.bicepparam, so the only thing under test is the
// module property that was flipped. If this file compiles, the gap is open.
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
param deployIdentityPrincipalId = '00000000-0000-0000-0000-000000000000'
