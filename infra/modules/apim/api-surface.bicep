// The MAIA API as API Management sees it: a named backend, one API bound to it,
// and an operation per MAIA route. Each operation carries a policy uploaded
// from infra/apim-policies/, so the routing and the throttling policy cannot
// drift apart — an operation with no policy would have no rate limit.
//
// The operation list is a parameter. Adding a route to MAIA is a one-line
// change in the parameters file, and a route that is NOT on the list is
// unreachable through APIM. That is the default this module is built to have.

targetScope = 'resourceGroup'

@description('Name of the APIM service that owns the API. Referenced as an existing resource: the service itself is created by infra/modules/apim/service.bicep in the same deployment, and Bicep `parent` can only bind to a declared resource.')
param apimServiceName string

@description('Name of the backend pointing at the MAIA container app.')
param backendName string

@description('Name of the API. Becomes the path segment after the gateway host, e.g. https://<gateway>/maia/query.')
param apiName string

@description('URL path prefix of the API at the gateway.')
param apiPath string

@description('Display name of the API in the portal and in traces.')
param apiDisplayName string

@description('Description of the API shown to consumers.')
param apiDescription string

@description('Base URL of the MAIA container app, e.g. "https://ca-maia-abc.eastasia.azurecontainerapps.io". Becomes the backend url and the API serviceUrl.')
param containerAppUrl string

@description('Directory (tenant) id. Used to build the OpenID Connect metadata URL that validate-jwt fetches the tenant signing keys from, and to pin the tid claim.')
param tenantId string

@description('Client id of the Entra application registration — the `aud` value every accepted token must carry.')
param entraApplicationClientId string

@description('Cloud login endpoint, e.g. "https://login.microsoftonline.com/". Passed in rather than hardcoded so the same templates work in Azure Government and in a private cloud.')
param entraLoginEndpoint string

@description('Browser origins allowed to call the API through the gateway. Must match the CORS_ORIGINS the container app itself is configured with; a mismatch shows up as a CORS error in the browser and a 200 in the logs.')
param allowedOrigins array

@description('API-wide rate limit in calls per minute per key. This is the outer envelope; per-operation limits are the inner ones and are tighter for the paid operations.')
param apiCallsPerMinute int = 600

@description('Operations to expose. Each entry becomes an operation plus its policy. `protected: true` applies operation-protected.xml (validate-jwt plus a per-key rate limit); false applies operation-public.xml. A route absent from this list returns 404 at the gateway.')
param apiOperations array

resource apimService 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

var apiInboundPolicy = replace(
  replace(
    loadTextContent('../../apim-policies/api-inbound.xml'),
    '{{ALLOWED_ORIGINS}}',
    join(map(allowedOrigins, origin => '<origin>${origin}</origin>'), '\n        ')
  ),
  '{{API_CALLS_PER_MINUTE}}',
  string(apiCallsPerMinute)
)

resource backend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apimService
  name: backendName
  properties: {
    description: 'MAIA container app on Azure Container Apps'
    url: containerAppUrl
    // HTTPS: the container app ingress sets allowInsecure: false, so an http
    // backend would be refused by the app. These two must be changed together.
    protocol: 'https'
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apimService
  name: apiName
  properties: {
    path: apiPath
    displayName: apiDisplayName
    description: apiDescription
    protocols: [
      'https'
    ]
    serviceUrl: containerAppUrl
    // false in V1: the only consumer is the MAIA front end, which
    // authenticates with an Entra token, not an APIM subscription key.
    // Enabling subscriptions would add a second, independently rotatable
    // credential to every request for no current consumer.
    subscriptionRequired: false
    isCurrent: true
  }

  resource apiPolicy 'policies@2024-05-01' = {
    name: 'policy'
    properties: {
      format: 'rawxml'
      value: apiInboundPolicy
    }
  }
}

// Each operation is a module instance rather than an element of a loop on this
// resource: Bicep forbids a nested resource inside a resource with a
// for-expression, and the operation policy has to be nested inside the
// operation. See infra/modules/apim/operation.bicep for the full reason.
module operations './operation.bicep' = [
  for op in apiOperations: {
    name: 'op-${op.name}'
    params: {
      apimServiceName: apimServiceName
      apiName: apiName
      operationName: op.name
      operationDisplayName: op.displayName
      operationDescription: op.description
      operationMethod: op.method
      operationUrlTemplate: op.urlTemplate
      operationProtected: op.protected
      rateLimitCalls: op.rateLimitCalls
      rateLimitWindowSeconds: op.rateLimitWindowSeconds
      tenantId: tenantId
      entraApplicationClientId: entraApplicationClientId
      entraLoginEndpoint: entraLoginEndpoint
    }
  }
]

@description('Gateway URL of the API, i.e. https://<gateway>/<apiPath>. A client configured with the Front Door host still has to be able to resolve this during CORS debugging.')
output apiGatewayUrl string = 'https://${apimService.properties.gatewayUrl}/${apiPath}'

@description('Name of the API, used by `az apim api show` in the verification checklist.')
output apiNameOutput string = api.name

@description('Names of the operations actually registered, so a verification step can assert the list rather than assume it.')
output operationNames array = map(apiOperations, op => op.name)
