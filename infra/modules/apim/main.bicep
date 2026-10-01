// The MAIA gateway: the API Management instance and the API surface published on
// it. One module for both, because they are one concern — a gateway without a
// routed API is an expensive no-op, and an API with no gateway has no rate
// limit, no JWT validation and no audit trail.

targetScope = 'resourceGroup'

@description('Deployment region. The APIM region cannot be changed after creation, and a backend in another region pays cross-region latency on every request.')
param location string

@description('Short environment name for the shared tag set.')
param environmentName string

@description('Owner contact recorded on the service and sent as the APIM notification sender.')
param ownerContact string

@description('Names of the gateway resources: service, backend, api, and the URL path prefix the API is published at.')
param resourceNames object

@description('APIM SKU. "Consumption" for V1. "Developer" for staging. "Premium" is required for the V5 private-injection path — see the tier decision comment in service.bicep.')
param skuName string

@description('APIM network configuration. "None" on Consumption; "External" once a Premium instance is injected into a VNet in V5.')
param virtualNetworkType string

@description('Base URL of the MAIA container app, e.g. "https://ca-maia-abc.eastasia.azurecontainerapps.io".')
param containerAppUrl string

@description('Directory (tenant) id, substituted into the validate-jwt required claim and into the OpenID Connect metadata URL.')
param tenantId string

@description('Client id of the Entra application registration — the audience every accepted token must carry.')
param entraApplicationClientId string

@description('Cloud login endpoint, e.g. "https://login.microsoftonline.com/".')
param entraLoginEndpoint string

@description('Browser origins allowed to call the API. Must match the CORS_ORIGINS the container app is configured with.')
param allowedOrigins array

@description('Operations exposed at the gateway with their per-operation rate limits and whether each requires an Entra token. A MAIA route absent from this list returns 404 at the gateway.')
param apiOperations array

@description('Resource id of the Log Analytics workspace receiving gateway logs, audit logs and metrics.')
param logAnalyticsWorkspaceId string

module service './service.bicep' = {
  name: 'apim-service'
  params: {
    location: location
    environmentName: environmentName
    ownerContact: ownerContact
    apimServiceName: resourceNames.service
    apimSkuName: skuName
    apimPublisherEmail: ownerContact
    apimPublisherName: 'MAIA Platform'
    apimVirtualNetworkType: virtualNetworkType
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
  }
}

module apiSurface './api-surface.bicep' = {
  name: 'apim-api-surface'
  // The operations are children of the service created above; without this the
  // two modules can be evaluated in parallel and the operations fail against a
  // service that does not exist yet.
  dependsOn: [
    service
  ]
  params: {
    apimServiceName: resourceNames.service
    backendName: resourceNames.backend
    apiName: resourceNames.api
    apiPath: resourceNames.apiPath
    apiDisplayName: 'MAIA API'
    apiDescription: 'MAIA retrieval-augmented generation assistant: /query, /chat, /chat/stream and the operational probes.'
    containerAppUrl: containerAppUrl
    tenantId: tenantId
    entraApplicationClientId: entraApplicationClientId
    entraLoginEndpoint: entraLoginEndpoint
    allowedOrigins: allowedOrigins
    apiOperations: apiOperations
  }
}

@description('Gateway URL of the MAIA API, e.g. "https://<gateway>/maia". This is one of the registered web redirect URIs on the Entra application.')
output gatewayUrl string = service.outputs.apimGatewayUrl

@description('Host name of the APIM gateway without a scheme.')
output gatewayHostName string = service.outputs.apimGatewayHostName

@description('Name of the APIM service, used by az apim commands in the runbook.')
output serviceName string = service.outputs.apimServiceNameOutput

@description('Resource id to use as the scope of a role assignment for the APIM system-assigned identity.')
output roleAssignmentScopeId string = service.outputs.apimRoleAssignmentScopeId

@description('Operations actually registered at the gateway, so a verification step can assert the list rather than assume it.')
output operationNames array = apiSurface.outputs.operationNames
