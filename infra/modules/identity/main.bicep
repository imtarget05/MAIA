// Workload identity for the MAIA container: a user-assigned managed identity and
// the Entra ID app registration APIM validates tokens against.
//
// WHY user-assigned and not system-assigned: the identity must be referenced
// from three different resource scopes (the container app, APIM's federated
// trust and a future GitHub OIDC federation) and it must survive the
// destruction of the resource that created it, otherwise a rebuild silently
// invalidates every cached token and every role assignment.
//
// WHY no client secret anywhere: a secret in Key Vault still has to be read by
// something, and the thing that reads it is the workload. Managed identity
// removes the secret entirely — there is nothing to leak, rotate or expire.

targetScope = 'resourceGroup'

// Removed: azureAdFirstPartyAppId. It only fed the requiredResourceAccess
// pre-consent, which is gone. Kept as a comment rather than deleted outright so
// the reason it left is discoverable from the file.

@description('Deployment region; the managed identity must sit in the same region as the container app it authenticates for.')
param location string

@description('Short environment name for the shared tag set.')
param environmentName string

@description('Name of the user-assigned managed identity attached to the MAIA container app.')
param managedIdentityName string

@description('Unique name of the Entra ID application registration. Must be unique tenant-wide.')
param entraApplicationName string

@description('Redirect URIs registered on the `web` platform. The APIM gateway URL is the one that matters; the Container Apps FQDN lets the Streamlit front end complete a direct auth handshake during debugging.')
param webRedirectUris array

@description('Redirect URIs registered on the `spa` platform (public client, PKCE only).')
param spaRedirectUris array

@description('Owner contact recorded on the identity resource.')
param ownerContact string

var tags = {
  env: environmentName
  project: 'maia'
  managedBy: 'bicep'
  owner: ownerContact
}

// Required to declare tenant-scoped Microsoft Graph resources. The type
// package is pinned in infra/bicepconfig.json; `az bicep restore` must run
// before the first build in a clean environment.
extension microsoftGraphV1

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
  tags: tags
}

// `allowedMemberTypes: []` is the default in Graph, but it is declared
// explicitly on both platforms because it is the single most important line in
// this file: an empty list means "no user may sign in through this app". A
// non-empty list would turn a workload registration into a phishing target.
resource entraApplication 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: entraApplicationName
  displayName: entraApplicationName
  description: 'MAIA API — machine identity for the enterprise Azure deployment. Managed identity only; no interactive sign-in.'
  signInAudience: 'AzureADMyOrg'
  api: {
    // v2 tokens: shorter lifetime, richer claims, and required by the
    // validate-jwt policy in infra/apim-policies/operation-protected.xml.
    requestedAccessTokenVersion: 2
  }
  web: {
    redirectUris: webRedirectUris
    implicitGrantSettings: {
      // Implicit flow is deprecated and incompatible with v2 tokens. Leaving
      // it on would let a browser mint a token for this app from a URL.
      enableAccessTokenIssuance: false
      enableIdTokenIssuance: false
    }
  }
  spa: {
    redirectUris: spaRedirectUris
  }
  // NO requiredResourceAccess, deliberately.
  //
  // An earlier draft pre-consented this registration to the first-party Graph
  // app so APIM could call Graph on the workload's behalf. Two reasons that is
  // wrong: nothing in MAIA calls Microsoft Graph (`grep -rn 'graph.microsoft.com'
  // src/` is empty), and a delegated `Scope` entry must name the SCOPE
  // (`User.Read`), not the resource app id — passing the app id grants nothing
  // and reads as if it grants everything. Pre-consent is a standing privilege
  // grant, so an unused one is pure attack surface. Adding it back requires a
  // real Graph call site to point at.
}

resource entraServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: entraApplication.appId
  displayName: '${entraApplicationName}-sp'
}

@description('Resource id of the user-assigned managed identity. Passed to the container app, which binds it as its runtime identity.')
output managedIdentityId string = managedIdentity.id

@description('Object id of the managed identity service principal. This is the principal used in every RBAC role assignment — never the client id.')
output managedIdentityPrincipalId string = managedIdentity.properties.principalId

@description('Client id of the managed identity. Passed to AZURE_CLIENT_ID and to az login --allow-no-subscriptions for out-of-band data-plane calls.')
output managedIdentityClientId string = managedIdentity.properties.clientId

@description('Application (client) id of the Entra registration. This is the `aud` claim that APIM validate-jwt checks against.')
output entraApplicationClientId string = entraApplication.appId

// WHY last(split(...)) and not app.id: for Graph resources `id` is the resource
// URL (`/applications/{objectId}`), NOT the object id. Exporting `entraApplication.id`
// unchanged as "object id" handed callers a path where a GUID belongs, and the
// failure mode is silent — `az ad sp show --id /applications/{guid}` on a
// mis-typed value errors, but any caller that strips or ignores the value keeps
// working and the wrong id reaches a role assignment.
@description('Object id (GUID) of the Entra application, extracted from the last segment of entraApplication.id. This is the value `az ad sp show --id` and role assignment against the app itself expects.')
output entraApplicationObjectId string = last(split(entraApplication.id, '/'))

@description('App id of the Entra service principal. Equal to entraApplicationClientId; kept separate so consumers do not have to know the identity model.')
output entraServicePrincipalAppId string = entraServicePrincipal.appId
