// API Management instance for the MAIA edge.
//
// TIER DECISION — Consumption.
//
//   Required by V1: validate-jwt against Entra, rate limiting, Key Vault as a
//   backend credential store, a managed identity of its own, and a WAF in
//   front of it. All five are GA on Consumption. The developer portal is the
//   only Consumption gap and it is switched off for a production deployment
//   anyway.
//
//   Consumption vs Developer: Developer has a fixed hourly capacity and
//   removes the per-request billing model, so a runaway client is billed by
//   the hour rather than capped. It also carries an SLA, which is an argument
//   for it only once there is a support contract to honour. Developer is the
//   correct answer for a staging environment, which is why skuName is a
//   parameter and main.parameters.dev.json sets it.
//
//   Consumption vs Premium: Premium is the only tier that can be injected into
//   a virtual network or reached over a private endpoint. That matters in V5,
//   not V1, and Premium costs roughly two orders of magnitude more per month.
//   The template therefore takes the SKU as a parameter rather than baking the
//   decision in: V5 is a one-line change in the parameters file, and the
//   `virtualNetworkType` parameter below already accepts the injected value.
//
//   Developer portal: off. A developer portal on a production APIM is a
//   publicly enumerable catalogue of the internal API surface, behind an
//   admin account that is the most phished credential in the deployment.

targetScope = 'resourceGroup'

@description('Deployment region. The APIM region cannot be changed after creation, and a backend in another region pays cross-region latency on every request.')
param location string

@description('Short environment name for the shared tag set.')
param environmentName string

@description('Owner contact recorded on the service. Also sent as the APIM notification sender.')
param ownerContact string

@description('Name of the APIM service. Globally unique.')
param apimServiceName string

@description('APIM SKU. "Consumption" for V1. "Developer" for staging. "Premium" is required for the V5 private-injection path.')
param apimSkuName string = 'Consumption'

@description('Capacity. 0 means serverless, which is the only valid value for Consumption and is ignored by Developer and Premium.')
param apimCapacity int = 0

@description('Email address recorded as the APIM publisher. API Management requires it and uses it for service notifications.')
param apimPublisherEmail string

@description('Display name recorded as the APIM publisher.')
param apimPublisherName string

@description('Network configuration. "None" is the only valid value on Consumption; "External" is the V5 value for a Premium instance with VNet injection and a public inbound path.')
param apimVirtualNetworkType string = 'None'

@description('Whether the gateway accepts traffic from the public internet. Kept true in V1 because the Front Door origin reaches APIM over its public endpoint; V5 sets this to false once the private link is approved.')
param apimPublicNetworkAccess bool = true

@description('Expose the developer portal. Left off: a public portal is an inventory of the internal API surface, and it is the credential most worth stealing in this deployment.')
param apimDeveloperPortalEnabled bool = false

@description('Resource id of the Log Analytics workspace receiving gateway logs and metrics.')
param logAnalyticsWorkspaceId string

var tags = {
  env: environmentName
  project: 'maia'
  managedBy: 'bicep'
  owner: ownerContact
}

resource apimService 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimServiceName
  location: location
  tags: tags
  identity: {
    // APIM needs its own identity to read Key Vault in V5 and to sign the
    // client certificate used for client-certificate authentication. Neither
    // is used in V1, but an identity cannot be added later to a Consumption
    // instance without a redeploy, so it is declared now.
    type: 'SystemAssigned'
  }
  sku: {
    name: apimSkuName
    capacity: apimCapacity
  }
  properties: {
    publisherName: apimPublisherName
    publisherEmail: apimPublisherEmail
    // Client certificates would be a second authentication path with its own
    // CA to operate. The Entra path is the only one in V1.
    enableClientCertificate: false
    publicNetworkAccess: apimPublicNetworkAccess ? 'Enabled' : 'Disabled'
    virtualNetworkType: apimVirtualNetworkType
    customProperties: {
      // HTTP/2 is not cosmetic here: /chat/stream is server-sent events, and
      // over HTTP/1.1 the gateway buffers the stream and the client sees one
      // response at the end instead of tokens as they are produced.
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Protocols.Http2': 'true'
      // A revocation check on every request would make the gateway fail every
      // token issued after a CA rotation. Revocation belongs in the token
      // lifetime, which is what the short v2 lifetime gives us.
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.ClientCertificateRevocationCheck': 'Ignore'
    }
  }
}

// No gateway log = no way to tell "the app rejected it" from "the gateway
// rejected it", which is the first question in every incident.
resource apimDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-apim'
  scope: apimService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'ApiManagementGatewayLogs'
        enabled: true
      }
      {
        category: 'ApiManagementGatewayLivelogs'
        enabled: true
      }
      {
        category: 'ApiManagementAuditLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

@description('Gateway hostname of the APIM instance, e.g. "apim-maia.azure-api.net". This is the Front Door origin for the V5 private-link path and the entry in the Entra redirect URI list.')
output apimGatewayUrl string = 'https://${apimService.properties.gatewayUrl}'

@description('Host name of the APIM gateway without a scheme.')
output apimGatewayHostName string = apimService.properties.gatewayUrl

@description('Name of the APIM service, used by az apim commands in the runbook.')
output apimServiceNameOutput string = apimService.name

@description('Resource id of the APIM service. Passed to the API surface module as the parent scope.')
output apimServiceId string = apimService.id

// WHY the service id and not an identity id: a system-assigned identity has no
// addressable ARM resource of its own, so a role assignment targeting it is
// scoped to the resource that carries it. The APIM instance id IS that scope.
@description('Resource id to use as the scope of any role assignment for the APIM system-assigned identity, e.g. a Key Vault Secrets User grant in a later phase.')
output apimRoleAssignmentScopeId string = apimService.id

@description('Whether the developer portal was deployed. Emitted so the runbook verification checklist can assert on it rather than on a human reading a screenshot.')
output apimDeveloperPortalDeployed bool = !empty(apimService.properties.developerPortalUrl) && apimDeveloperPortalEnabled
