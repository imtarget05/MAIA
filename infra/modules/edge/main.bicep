// Front Door Premium + WAF, the public edge in front of the MAIA container app.
//
// VERDICT on AFD -> Container Apps over Private Link: supported and used here.
// Evidence, all from Microsoft documentation and the live `az afd` surface
// rather than from a sample manifest:
//   * "Access an Azure container app using an Azure Front Door with Private
//     Link" (learn.microsoft.com/azure/container-apps/how-to-integrate-with-
//     azure-front-door) states the target sub-resource is `managedEnvironments`
//     and the origin host name is the *container app* FQDN.
//   * The same page states the feature "is only supported for workload profile
//     environments" — which is why the environment in infra/modules/apps
//     declares explicit workloadProfiles.
//   * `az afd origin create` accepts --private-link-sub-resource-type
//     managedEnvironments and --private-link-resource <environment id>, which
//     is exactly the sharedPrivateLinkResource shape used below.
// Two documented constraints the runbook has to carry:
//   * The private endpoint connection AFD creates arrives PENDING on the
//     environment and must be approved by an operator. Until it is, requests
//     through the AFD endpoint fail. `az bicep build` cannot express that, so
//     docs/deployment-azure.md §7 carries the approval command.
//   * Private endpoints on Container Apps are HTTP-only. The route therefore
//     supports Http+Https with an httpsRedirect, which means AFD always
//     originates TLS to the app and the app's allowInsecure:false holds.

targetScope = 'resourceGroup'

@description('Deployment region for the profile, endpoint and WAF policy. AFD is regional and its region cannot be changed after creation.')
param location string

@description('Short environment name for the shared tag set.')
param environmentName string

@description('Owner contact recorded on the Front Door resources.')
param ownerContact string

@description('Name of the Front Door CDN profile. Globally unique.')
param frontDoorProfileName string

@description('Name of the Front Door endpoint. Globally unique; its hostname is the public MAIA origin.')
param frontDoorEndpointName string

@description('Name of the origin group holding the Container Apps origin.')
param frontDoorOriginGroupName string

@description('Name of the Container Apps origin inside the origin group.')
param frontDoorOriginName string

@description('Name of the route mapping the endpoint to the origin group.')
param frontDoorRouteName string

@description('Name of the WAF policy.')
param frontDoorWafPolicyName string

@description('Name of the security policy that associates the WAF policy with the endpoint domain.')
param frontDoorSecurityPolicyName string

@description('Ingress FQDN of the MAIA container app, used as the origin host name and origin host header.')
param containerAppFqdn string

@description('Resource id of the Container Apps environment. This is the privateLinkResourceId: the private endpoint is created against the environment, while containerAppFqdn is only the host it serves.')
param containerAppsEnvironmentId string

@description('Requests per minute per client IP that the WAF rate-limit rule allows. The rule is a coarse edge guard against a single host hammering the origin; the per-API-key limits in APIM are the real quota.')
param wafRateLimitPerMinute int = 600

@description('WAF mode. "Prevention" blocks matched rules; "Detection" only logs. Start in Detection for a week against real traffic if false positives are a concern, then switch.')
param wafMode string = 'Prevention'

@description('Enable the managed bot protection rule set. Bad bots are blocked, unknown bots are logged: an RAG endpoint with no bot control is a scraping target because responses are free to reuse.')
param enableBotProtection bool = true

@description('Resource id of the Log Analytics workspace receiving Front Door WAF and access logs.')
param logAnalyticsWorkspaceId string

@description('Path the origin health probe requests. /health is the liveness endpoint, which by design loads no dependency — a probe on /ready would flap every time the vector store is slow.')
param healthProbePath string = '/health'

var tags = {
  env: environmentName
  project: 'maia'
  managedBy: 'bicep'
  owner: ownerContact
}

resource frontDoorProfile 'Microsoft.Cdn/profiles@2024-09-01' = {
  name: frontDoorProfileName
  location: location
  tags: tags
  sku: {
    // Premium, not Standard. Private link to origins and the managed WAF
    // rule set are Premium-only; Standard supports custom WAF rules alone.
    name: 'Premium_AzureFrontDoor'
  }
}

resource frontDoorEndpoint 'Microsoft.Cdn/profiles/afdEndpoints@2024-09-01' = {
  parent: frontDoorProfile
  name: frontDoorEndpointName
  location: location
  properties: {
    enabledState: 'Enabled'
  }
}

// Bicep ships no type definitions for AFD originGroups, origins or afdWafPolicies
// at ANY apiVersion (verified 2023-05-01 through 2026-04-01-preview), so BCP081
// is suppressed on those three declarations rather than worked around. The
// property names below are taken from the documented `az afd origin-group /
// az afd origin / az afd waf-policy` parameter surface, not from guesswork.
#disable-next-line BCP081
resource frontDoorOriginGroup 'Microsoft.Cdn/profiles/afdEndpoints/originGroups@2024-09-01' = {
  parent: frontDoorEndpoint
  name: frontDoorOriginGroupName
  properties: {
    healthProbeSettings: {
      probePath: healthProbePath
      probeRequestType: 'GET'
      // HTTPS to the origin. The route force-redirects, so AFD only ever
      // speaks TLS here, which is what lets the app keep allowInsecure:false.
      probeProtocol: 'Https'
      probeIntervalInSeconds: 30
    }
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
      additionalLatencyInMilliseconds: 50
    }
  }
}

#disable-next-line BCP081
resource frontDoorOrigin 'Microsoft.Cdn/profiles/afdEndpoints/originGroups/origins@2024-09-01' = {
  parent: frontDoorOriginGroup
  name: frontDoorOriginName
  properties: {
    hostName: containerAppFqdn
    originHostHeader: containerAppFqdn
    // Port 80 is listed because ACA requires both ports on a private-link
    // origin, but it is never used: httpsRedirect + MatchRequest mean AFD
    // always forwards to 443.
    httpPort: 80
    httpsPort: 443
    priority: 1
    weight: 1000
    enabledState: 'Enabled'
    // The private link itself. `privateLink.id` is the resource the endpoint is
    // created against — the managed ENVIRONMENT, not the container app — which
    // is why containerAppsEnvironmentId is threaded into this module while
    // containerAppFqdn is only the host name that endpoint serves.
    // privateLinkLocation is the region of the private endpoint.
    sharedPrivateLinkResource: {
      groupId: 'managedEnvironments'
      privateLink: {
        id: containerAppsEnvironmentId
      }
      privateLinkLocation: location
      requestMessage: 'AFD Private Link Request'
    }
  }
}

resource frontDoorRoute 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-09-01' = {
  parent: frontDoorEndpoint
  name: frontDoorRouteName
  properties: {
    originGroup: {
      id: frontDoorOriginGroup.id
    }
    forwardingProtocol: 'MatchRequest'
    linkToDefaultDomain: 'Enabled'
    // Force HTTPS at the edge. This is the reason allowInsecure:false on the
    // container app is reachable at all.
    httpsRedirect: 'Enabled'
    // Http is listed only because the private endpoint to a Container Apps
    // environment is HTTP-only; the redirect above means it never carries
    // production traffic.
    supportedProtocols: [
      'Http'
      'Https'
    ]
    patternsToMatch: [
      '/*'
    ]
  }
}

#disable-next-line BCP081
resource frontDoorWafPolicy 'Microsoft.Cdn/profiles/afdWafPolicies@2024-09-01' = {
  parent: frontDoorProfile
  name: frontDoorWafPolicyName
  properties: {
    policySettings: {
      policyType: 'WebApplicationFirewall'
      enabledState: 'Enabled'
      mode: wafMode
    }
    managedRules: {
      // concat rather than a spread: the conditional element makes the union
      // type unassignable to a fixed array, and concat produces exactly the
      // one- or two-element list the API expects.
      managedRuleSets: concat(
        [
          // DRS 1.0 is the oldest Front Door rule set still in service. The
          // WAF rule set support policy stops accepting DRS 1.0 for NEW
          // policies from 2026-06-01 and ends support for existing ones on
          // 2027-02-26, so a later phase must move this to 2.1. 1.0 is used
          // here because it is the combination validated by the deployment
          // what-if in docs/deployment-azure.md §7.
          {
            managedRuleSetType: 'DefaultRuleSet'
            ruleSetVersion: '1.0'
          }
        ],
        // Bot protection. There is no "HighAlert" action anywhere in the AFD
        // WAF schema — the documented action values are Allow, Block and Log —
        // so severity is expressed per bot category instead. BadBots are
        // blocked and UnknownBots are logged: the closest faithful reading of
        // "HighAlert" is to block what is known-bad and observe what is merely
        // unrecognised, rather than lock out an unknown uptime monitor.
        enableBotProtection ? [
          {
            managedRuleSetType: 'Microsoft_BotManagerRuleSet'
            ruleSetVersion: '1.0'
            ruleGroupOverrides: [
              {
                ruleGroupName: 'BadBots'
                action: 'Block'
              }
              {
                ruleGroupName: 'UnknownBots'
                action: 'Log'
              }
            ]
          }
        ] : []
      )
    }
    customRules: [
      {
        name: 'per-client-rate-limit'
        priority: 10
        // SQLi, XSS, LFI and RCE are already covered by the managed default
        // rule set, so no duplicate custom rules are added for them. The one
        // thing the managed set does not do is bound a single client, which is
        // exactly the abuse a per-key RAG quota is meant to stop.
        ruleType: 'RateLimitRule'
        rateLimitDurationInMinutes: 1
        rateLimitThreshold: wafRateLimitPerMinute
        action: {
          actionType: 'Block'
        }
        matchConditions: [
          {
            matchVariable: 'RemoteAddr'
            operator: 'IPMatch'
            // Any client. A rule with no exclusions is the point: the threshold
            // is per source IP over a rolling minute, so a shared NAT egress is
            // the known false-positive source and is handled in the runbook.
            matchValue: [
              '0.0.0.0/0'
              '::/0'
            ]
            transforms: [
              'Lowercase'
            ]
          }
        ]
      }
    ]
  }
}

resource frontDoorSecurityPolicy 'Microsoft.Cdn/profiles/securityPolicies@2024-09-01' = {
  parent: frontDoorProfile
  name: frontDoorSecurityPolicyName
  properties: {
    parameters: {
      type: 'WebApplicationFirewall'
      wafPolicy: {
        id: frontDoorWafPolicy.id
      }
      associations: [
        {
          domains: [
            {
              id: frontDoorEndpoint.id
            }
          ]
          patternsToMatch: [
            '/*'
          ]
        }
      ]
    }
  }
}

// WAF decisions and access logs are the only record of what the edge blocked.
resource frontDoorDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-frontdoor'
  scope: frontDoorProfile
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'FrontDoorWebApplicationFirewallLog'
        enabled: true
      }
      {
        category: 'FrontDoorAccessLog'
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

@description('Public hostname of the Front Door endpoint, e.g. "fdo-maia-abc123.b01.azurefd.net". This is the only MAIA URL a browser or client should ever be pointed at.')
output frontDoorEndpointHostName string = frontDoorEndpoint.properties.hostName

@description('HTTPS URL of the Front Door endpoint, the canonical public MAIA base URL.')
output frontDoorUrl string = 'https://${frontDoorEndpoint.properties.hostName}'

@description('Resource id of the Front Door profile. Scope for any future Front Door role assignment.')
output frontDoorProfileId string = frontDoorProfile.id

@description('Name of the origin group, needed by the runbook step that inspects origin health.')
output frontDoorOriginGroupNameOutput string = frontDoorOriginGroup.name
