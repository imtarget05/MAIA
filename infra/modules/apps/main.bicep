// ACR + Container Apps environment + the MAIA container app.
//
// Image source: the image is built and pushed to GHCR by
// .github/workflows/build-container.yml, not to this registry. ACR is created
// because phase 2 (Blob/EventGrid) and the V5 private-endlink plan need a
// first-party registry, and because a second registry is cheaper than a
// migration later. Nothing in V1 pushes to it; AcrPull is still granted so the
// identity can pull once V5 flips the ingress path.

targetScope = 'resourceGroup'

@description('Deployment region for the registry, the environment and the app. All three must share a region: Container Apps cannot pull from a cross-region ACR over a private endpoint.')
param location string

@description('Short environment name for the shared tag set.')
param environmentName string

@description('Owner contact recorded on every resource here.')
param ownerContact string

@description('Globally unique registry name, e.g. "acrMaia".')
param containerRegistryName string

@description('Registry SKU. Premium is required for private endpoints and zone redundancy in V5; Standard cannot be migrated up in place without a new registry name in some paths, so starting at Premium avoids a second registry later.')
param containerRegistrySku string = 'Premium'

@description('Name of the Container Apps managed environment.')
param containerAppsEnvironmentName string

@description('Name of the workload profile the MAIA app is placed on.')
param workloadProfileName string = 'default'

@description('Name of the second workload profile, reserved for the phase-2 jobs workload. Declared but unused: a container app is not created on it in V1.')
param reservedJobsWorkloadProfileName string = 'jobs'

@description('Zone redundancy for the environment. Not supported on the consumption plan, so it stays false in V1; raise it only together with a dedicated plan.')
param zoneRedundantEnvironment bool = false

@description('Subnet resource id delegated to Microsoft.App for the environment infrastructure. Empty in V1; supplying it moves the environment onto a VNet (V5).')
param infrastructureSubnetResourceId string = ''

@description('Name of the MAIA container app.')
param containerAppName string

@description('Container image the app runs. Defaults to the GHCR image produced by .github/workflows/build-container.yml. Never pin a digest in this parameter file: a digest pin would make every redeploy a template edit instead of a revision rollout.')
param containerImage string

@description('Container name inside the revision. Kept as a parameter because the Container Apps log stream and the App Insights container field are keyed on it.')
param containerName string = 'maia-api'

@description('Container memory ceiling. 1Gi is the tuned size for the MAIA image and must not be reduced: the retrieval stack plus the in-process auth database OOM below it, and an OOM during model load shows up as a crash loop with no probe failure.')
param containerMemory string = '1Gi'

@description('Container vCPU as a JSON number string. 0.5 is the tuned value. See the note above the vcpu var for why this is not a bare Bicep decimal literal.')
param containerVcpu string = '0.5'

@description('Minimum replicas. 1 keeps the app warm; 0 would make the first request after idle pay a cold start, which on a RAG app is a multi-second LLM queue.')
param minReplicas int = 1

@description('Maximum replicas. 10 is a cost ceiling, not a throughput target: the auth database is sqlite in this image, so replicas do not share session state until phase 3 moves it to Postgres.')
param maxReplicas int = 10

@description('Ingress transport. "auto" negotiates HTTP/2, which /chat/stream needs for server-sent events; "http1" would buffer the stream.')
param ingressTransport string = 'auto'

@description('Target port. Must match the EXPOSE in Dockerfile.api, which is 8000.')
param targetPort int = 8000

@description('Whether the container app accepts traffic from the public internet. Left true in V1 because the CI verify job and the AFD private-endpoint approval both need to reach the app; V5 sets this to false once the AFD private link is approved.')
param externalIngress bool = true

@description('Reject plain HTTP at the app ingress. The Front Door route force-redirects HTTPS, so AFD only ever talks to the origin on 443 and this stays satisfiable.')
param allowInsecureIngress bool = false

@description('URI of the Key Vault holding the runtime secrets, with a trailing slash.')
param keyVaultUri string

@description('Key Vault secret names the app resolves. By contract each name IS the MAIA environment variable name, so one array produces both the Container Apps secret reference and the env var that consumes it. Order matches src/maia/azure_identity.py KV_SECRET_NAMES; changing it here without changing it there silently changes which secrets the app looks for.')
param keyVaultSecretNames array

@description('Plain environment variables for the app. Deliberately not a secret: the list is exactly the non-sensitive configuration, so it is safe to read in a PR.')
param plainEnvironmentVariables array

@description('Revision suffix. Bumping it forces a new revision, which is how a config-only change reaches a running ACA app.')
param revisionSuffix string

@description('Resource id of the user-assigned managed identity bound to the app.')
param managedIdentityId string

@description('Resource id of the Log Analytics workspace that receives container stdout and system logs.')
param logAnalyticsWorkspaceId string

var tags = {
  env: environmentName
  project: 'maia'
  managedBy: 'bicep'
  owner: ownerContact
}

// WHY json() and not the literal 0.5: the Bicep CLI shipped for arm64 macOS
// cannot lex decimal literals at all — it parses `0` and then treats `.5` as a
// property access (BCP020/BCP055), reproduced on 0.30, 0.36, 0.41 and 0.47.
// The ARM json() expression evaluates to the number 0.5 at deployment time, so
// the emitted template is correct. Replace with the literal once the lexer
// regression is fixed; a reviewer on a working toolchain can verify by
// swapping this line and re-running az bicep build.
var containerVcpuJson = json(containerVcpu)

// One entry per secret name, so a mismatch between the secret reference and the
// env var that reads it is impossible to express.
var kvSecretDefinitions = map(keyVaultSecretNames, name => {
  name: name
  keyVaultUrl: '${keyVaultUri}secrets/${name}'
  identity: managedIdentityId
})

var kvSecretEnvVars = map(keyVaultSecretNames, name => {
  name: name
  secretRef: name
})

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: containerRegistryName
  location: location
  tags: tags
  sku: {
    name: containerRegistrySku
  }
  properties: {
    // Admin user off: it is a shared static password with no expiry and no
    // per-identity attribution. Anonymous pull is already off by default for a
    // new registry and has no property in the ARM registry schema, so it is
    // asserted in the runbook's verification checklist instead of pretended
    // to be configured here.
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    policies: {
      // A registry that pushes a vulnerable base image has to be able to hold
      // it back. Quarantine plus trust policy (signed images only) turns "we
      // reviewed it once" into an enforced property.
      quarantinePolicy: {
        status: 'enabled'
      }
      trustPolicy: {
        status: 'enabled'
      }
      retentionPolicy: {
        days: 30
        status: 'enabled'
      }
    }
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    zoneRedundant: zoneRedundantEnvironment
    workloadProfiles: [
      {
        name: workloadProfileName
        // Consumption workload profiles are NOT size-parameterised in the
        // environment: the schema exposes only workloadProfileType plus counts.
        // The 1 GiB / 0.5 vCPU budget therefore lives on the container spec
        // below, where the platform actually enforces it.
        workloadProfileType: 'Consumption'
      }
      {
        name: reservedJobsWorkloadProfileName
        // Reserved for the phase-2 jobs workload. Declaring the profile now
        // means phase 2 adds a container app and nothing else.
        workloadProfileType: 'Consumption'
      }
    ]
    // V5 extension point. vnetConfiguration is only valid on a
    // subnet-delegated VNet, so it is spread in only when a subnet id is
    // supplied; emitting an empty object would be rejected by ARM. Flip
    // infrastructureSubnetResourceId in the parameters file and this is the
    // only line that has to change.
    ...(!empty(infrastructureSubnetResourceId) ? {
      vnetConfiguration: {
        infrastructureSubnetId: infrastructureSubnetResourceId
      }
    } : {})
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    // The key must be the identity resource id, not a friendly alias, and the
    // value must stay an empty object so the identity is bound but not
    // configured. The identity reference must be computable before the
    // deployment starts, which is why the id is threaded through as a string.
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    workloadProfileName: workloadProfileName
    configuration: {
      // Single active revision: rolling two revisions behind one hostname on a
      // sqlite-backed session store would split sessions across revisions.
      activeRevisionsMode: 'single'
      ingress: {
        external: externalIngress
        targetPort: targetPort
        transport: ingressTransport
        allowInsecure: allowInsecureIngress
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      // Secretless: ACA resolves each value from Key Vault at start-up using the
      // bound identity. No secret value is ever in the ARM template, in the
      // revision, or in `az containerapp show`.
      secrets: kvSecretDefinitions
    }
    template: {
      containers: [
        {
          // Tag, not digest: creating a revision re-resolves the tag, which is
          // ACA's equivalent of a pull policy of Always. Pinning a digest here
          // would be IfNotPresent and would require a template edit per build.
          image: containerImage
          name: containerName
          env: concat(plainEnvironmentVariables, kvSecretEnvVars)
          resources: {
            cpu: containerVcpuJson
            memory: containerMemory
          }
          probes: [
            {
              // /health deliberately loads no ML stack. A probe that imports
              // the retriever is the standard way to get an OOM kill in a
              // 1 GiB profile, and an OOM during a probe does not surface as a
              // probe failure.
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: targetPort
              }
            }
            {
              // /ready does check dependencies, so it is readiness-only: a
              // degraded dependency should stop new traffic, not restart the
              // process.
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: targetPort
              }
            }
          ]
        }
      ]
      revisionSuffix: revisionSuffix
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                // 50 in flight per replica. Above this the retrieval queue
                // backs up in memory and /ready starts reporting degraded
                // before the app returns errors.
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

// Container stdout and the ACA system log categories. Without these the only
// evidence of a start-up failure is an HTTP 503 from the ingress.
resource containerAppDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-containerapp'
  scope: containerApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'ContainerAppConsoleLogs_CL'
        enabled: true
      }
      {
        category: 'ContainerAppSystemLogs_CL'
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

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-containerappenv'
  scope: containerAppsEnvironment
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'ContainerAppSystemLogs_CL'
        enabled: true
      }
    ]
  }
}

resource registryDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-acr'
  scope: containerRegistry
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'ACRRepositoryEvents'
        enabled: true
      }
    ]
  }
}

@description('Login server of the registry, e.g. "acrMaia.azurecr.io". Passed to `az acr login` in phase 2.')
output containerRegistryLoginServer string = containerRegistry.properties.loginServer

@description('Resource id of the registry. Scope for the AcrPull role assignment.')
output containerRegistryId string = containerRegistry.id

@description('Ingress FQDN of the container app. This is the ServiceUrl of the APIM backend and the origin host of the Front Door route.')
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn

@description('Full HTTPS URL of the container app, built from the FQDN so callers do not have to remember the scheme.')
output containerAppUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'

@description('Resource id of the Container Apps environment. This — not the container app — is the privateLinkResourceId of the Front Door origin: the private endpoint is created against the environment and the app FQDN is only the host name it serves.')
output containerAppsEnvironmentId string = containerAppsEnvironment.id

@description('Default domain of the Container Apps environment.')
output containerAppsEnvironmentDefaultDomain string = containerAppsEnvironment.properties.defaultDomain

@description('Resource id of the container app.')
output containerAppId string = containerApp.id
