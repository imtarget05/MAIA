// Log Analytics workspace + Application Insights component for the MAIA stack.
//
// WHY this is in the V1 deployment even though observability is nominally a
// later phase: every other V1 resource is unverifiable without it. A container
// app that boots but answers 503 has no other evidence trail, and "the deploy
// looked fine" is not a verification. The workspace is the cheapest possible
// starting point — ingestion-only, no dashboards or saved searches, which is
// where the cost driver lives.

targetScope = 'resourceGroup'

@description('Deployment region. Log Analytics is regional and the region cannot be changed after creation.')
param location string

@description('Short environment name for the shared tag set.')
param environmentName string

@description('Name of the Log Analytics workspace. Must be globally unique.')
param logAnalyticsWorkspaceName string

@description('Name of the Application Insights component.')
param applicationInsightsName string

@description('Owner contact recorded on both resources.')
param ownerContact string

@description('Days of data retained in the workspace. Ingestion continues past this window and older data becomes inaccessible, which is the cost control that matters.')
param logRetentionInDays int = 30

var tags = {
  env: environmentName
  project: 'maia'
  managedBy: 'bicep'
  owner: ownerContact
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionInDays
    features: {
      // Removes the per-workspace RBAC exception. With this on, reading a
      // workspace requires a role assignment, so an operator who can read the
      // resource group cannot silently read the logs.
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    // Workspace-based: classic Application Insights had a separate data store
    // and separate pricing, so the same telemetry was billed twice. This is the
    // only supported shape going forward.
    WorkspaceResourceId: logAnalytics.id
  }
}

resource workspaceDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-workspace-self'
  scope: logAnalytics
  properties: {
    workspaceId: logAnalytics.id
  }
}

resource insightsDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-insights-self'
  scope: applicationInsights
  properties: {
    workspaceId: logAnalytics.id
  }
}

@description('Resource id of the Log Analytics workspace. Target of every diagnostic setting in this stack.')
output logAnalyticsWorkspaceId string = logAnalytics.id

@description('Name of the Log Analytics workspace, for `az monitor log-analytics workspace show`.')
output logAnalyticsWorkspaceNameOutput string = logAnalytics.name

@description('Resource id of the Application Insights component. Passed as the ACA OTLP exporter target in V2.')
output applicationInsightsId string = applicationInsights.id

@description('Instrumentation key of the Application Insights component. Exposed for completeness; the Azure Monitor OTLP path in V2 uses the connection string instead.')
output applicationInsightsInstrumentationKey string = applicationInsights.properties.InstrumentationKey
