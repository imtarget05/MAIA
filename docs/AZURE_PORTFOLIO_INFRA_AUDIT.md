# AZURE PORTFOLIO INFRA AUDIT

```text
audit_date  : 2026-10-02
method      : READ-ONLY. No apply, no create, no role change, no quota request.
commands    : az account/resource/vm list-usage/vm list-skus/provider list/
              consumption usage/ad app/federated-credential/storage account show,
              gh api (repository trees only)
mutations   : NONE
```

## 1. Subscription

| Field | Value |
|---|---|
| Subscription | `a3deec78-7edb-41cd-9e94-ec1d4d9379f5` ("Azure subscription 1") |
| State | Enabled |
| Tenant | `aa79a92c-ec09-4de1-baa9-151b8f9df886` |
| Subscriptions accessible | **1** (no ambiguity) |
| Operator identity | `28e48323-…` — `binhtan5734_gmail.com#EXT#@binhtan5734gmail.onmicrosoft.com` |
| Operator role | **Owner** @ subscription scope |

## 2. Regions in use

| Region | Resources | Notes |
|---|---|---|
| `eastasia` | 7 | All pre-existing portfolio evidence; AKS target region per ADR-012 |
| `southeastasia` | 1 | `rg-maia-tfstate` (new) |

## 3. Regional compute quota — MEASURED

```text
eastasia:
  Total Regional vCPUs        0 used / 10 limit
  Total low-priority vCPUs   0 used /  3 limit
  cloudServices (AKS)         0 used / 2500 limit

  standardDSv6Family          0 / 10     <- AKS uses Dsv6
  standardDSv5Family          0 /  0     <- BLOCKED
  standardDASv5Family         0 /  0     <- BLOCKED
  standardDv6Family           0 / 10
  standardDsv3Family          0 / 10
  standardBSFamily            0 / 10
  standardPBSFamily           0 /  6
```

**The "10 vCPU" in older reports is correct and current** — but it is the
*total regional* ceiling and it is **currently 0/10 used**, i.e. nothing is
holding compute quota.

### Family availability is the real constraint

`standardDSv6Family` and `standardDv6Family` are at 10. `standardDSv5Family` and
`standardDASv5Family` are at **0** — those families cannot be deployed at all in
this subscription today, regardless of total quota. AKS-SRE-Platform ADR-012
already records this as the reason the node SKU moved from `D4as_v5` to
`D4s_v6`.

## 4. SKU availability (eastasia) — separate from quota

| SKU | vCPU | RAM | Restrictions |
|---|---|---|---|
| `Standard_D2s_v6` | 2 | 8 GB | **NONE** |

A **quota-available** SKU is not automatically **capacity-available**. For
`D2s_v6` both are true here. `D2as_v5` would be quota-*unavailable* (family 0)
regardless of capacity.

## 5. Existing resource inventory — 8 resources total

| RG | Region | Resource | Type | Cost class |
|---|---|---|---|---|
| `NetworkWatcherRG` | eastasia | `NetworkWatcher_eastasia` | network watcher | free |
| `rg-maia-tfstate` | southeastasia | `sttfmaia` | storage (Standard_LRS) | **low, persistent** |
| `rg-portfolio-evidence` | eastasia | `workspace-rgportfolioevidenceEI7x` | Log Analytics | ingestion-billed |
| `rg-portfolio-evidence` | eastasia | `cae-portfolio` | ACA managed environment | **consumption-billed** |
| `rg-portfolio-evidence` | eastasia | `ca-maia-api` | Container App | consumption-billed |
| `rg-portfolio-evidence` | eastasia | `ca-helpdesk-portal` | Container App | consumption-billed |
| `rg-portfolio-evidence` | eastasia | `ca-factory-api` | Container App | consumption-billed |
| `rg-portfolio-evidence` | eastasia | `ca-creditflow-api` | Container App | consumption-billed |

### Ownership classification

| Class | Resources |
|---|---|
| SHARED_FOUNDATION | `sttfmaia` (tfstate) |
| PREEXISTING_OTHER | `NetworkWatcherRG/*`, all 6 `rg-portfolio-evidence` resources |
| MAIA / HELPDESK / FACTORY / AKS-SRE | **none currently exist** |

**Nothing in this subscription is owned by the four target projects except the
MAIA Terraform state store.** The `rg-portfolio-evidence` Container Apps are
pre-existing portfolio evidence, not any of the four projects' deployments —
attributing them to MAIA/Helpdesk/Factory on the strength of their *names* would
be exactly the misattribution this audit exists to prevent.

`ca-maia-api` is at `latestReadyRevision: ca-maia-api--0000012` on env
`cae-portfolio` (`wittysand-b748274c.eastasia.azurecontainerapps.io`). This
matches the "two unresolved revision identities" carried forward in MAIA's
`CURRENT-STATE.md`, so it is the same deployment, not a new one.

### Empty resource groups

`rg-maia-verify` — created by MAIA bootstrap, **empty by design**, holds the CI
## 6. Resource group inventory

| RG | Region | Tags | Resources | State |
|---|---|---|---|---|
| `NetworkWatcherRG` | eastasia | — | 1 | platform |
| `rg-maia-tfstate` | southeastasia | project/managedBy/owner | 1 | **active** |
| `rg-maia-verify` | southeastasia | project/managedBy/owner | 0 | **active, empty by design** |
| `rg-portfolio-evidence` | eastasia | — | 6 | pre-existing |

Nothing deleted. Nothing looks stale.

## 7. Provider registrations

| Provider | State |
|---|---|
| ContainerService, ContainerRegistry, ManagedIdentity, KeyVault, Network, Storage, OperationalInsights, App, DBforPostgreSQL, Cache, ServiceBus, Consumption | **REGISTERED** |
| `Microsoft.Insights` | **NOT_REGISTERED** |
| `Microsoft.Monitor` | **NOT_REGISTERED** |
| `Microsoft.Dashboard` | **NOT_REGISTERED** |

These three are exactly what Managed Prometheus / Azure Monitor metrics /
Managed Grafana need. They are **not registered**, and registering them is a
mutation — deferred, not done here. This is a real prerequisite finding for the
AKS observability profile.

## 8. Terraform state matrix

| Project | Backend config | State blob | OIDC identity | Canonical authority |
|---|---|---|---|---|
| **MAIA** | 3 × `backend.hcl`, `use_azuread_auth` | `maia/{dev,validation,prod}.terraform.tfstate` | `maia-github-oidc` + `azure-verify` env | **REMOTE (azurerm)** |
| **AKS-SRE-Platform** | none | none | none | **none — local state** |
| **Enterprise-IT-Helpdesk-Lab** | none | none | none | **none — local state** |
| **Factory** | none (`environments/*/terraform.tfvars` only) | none | none | **none — local state** |

Only MAIA has a real remote backend. The other three would write `.tfstate` into
the working directory on first apply — a blocker for any deployment attempt,
not a nice-to-have.

## 9. Identity / OIDC inventory

| Item | Value |
|---|---|
| Entra apps in tenant | **1** — `maia-github-oidc` (`4ba90267-…`) |
| Federated credentials | 1 — `github-actions-azure-verify` |
| issuer | `https://token.actions.githubusercontent.com` |
| subject | `repo:imtarget05@163159731/MAIA@1357198812:environment:azure-verify` |
| audience | `api://AzureADTokenExchange` |
| passwordCredentials | **0** |
| keyCredentials | **0** |
| Managed identities | 0 (none exist yet) |

RBAC on the GitHub service principal — exactly 3 assignments, all narrow:

| Role | Scope |
|---|---|
| `Contributor` | `…/resourceGroups/rg-maia-verify` |
| `Reader` | `…/resourceGroups/rg-maia-tfstate` |
| `Storage Blob Data Contributor` | `…/resourceGroups/rg-maia-tfstate` |

**Security warnings — none raised:**

- No client secrets anywhere. ✅
- No wildcard federated subject. ✅
- No Owner for the CI identity. ✅
- No subscription-wide Contributor for the CI identity. ✅
- No cross-project identity reuse (there are no other identities). ✅

## 12. Project requirements

### AKS-SRE-Platform — computed from its own Terraform

Read from `terraform/aks-foundation/variables.tf` and `main.tf`:

| Parameter | Repo default | vCPU |
|---|---|---|
| region | `eastasia` | — |
| system pool SKU | `Standard_D4s_v6` | 4 |
| system pool node count | **2** | **8** |
| workload pool SKU | `Standard_D2s_v6` | 2 |
| workload pool node count | 1, `auto_scaling_enabled = false` | **2** |
| **total initial** | | **10 vCPU** |

### This is the critical finding

```text
Total Regional vCPU quota  : 10
AKS-SRE initial requirement: 10 vCPU (2×D4s_v6 + 1×D2s_v6)
Headroom                   :  0
```

**AKS-SRE as currently configured would consume 100% of the subscription's
entire regional vCPU quota on its initial apply.** With autoscaling disabled on
the user pool there is nowhere to grow — but there is also nothing left for any
other project, and nothing left for AKS itself if a node is replaced or
rescheduled.

Three consequences that follow from the measurement:

1. **Sequential deployment is mandatory, not merely advisable.** AKS at 10/10
   leaves literally zero quota for project #2.
2. **No autoscaling demonstration is possible** in this configuration. An
   autoscaler proof needs headroom to scale *into*; 0 vCPU headroom means the
   experiment cannot run at all.
3. **Do not request quota yet** — see §14.

### The quota-vs-pods distinction (explicit, as required)

> Prometheus requesting 1.5 CPU does **not** consume 1.5 Azure regional vCPU.

Azure compute quota is consumed by **VM/node vCPU allocation only**. Pods
consume capacity *inside* already-allocated nodes.

| Layer | What it is | How it is measured |
|---|---|---|
| **A. Azure regional quota** | VM/node vCPU allocations | `az vm list-usage` → `cores`, `standardDSv6Family` |
| **B. Kubernetes schedulable** | node allocatable minus kubelet/system reservation | `kubectl describe node` → `Allocatable`, only after the cluster exists |
| **C. Pod requests/limits** | what workloads declare | `kubectl get pods -o json` |

Layer B **cannot** be measured before the cluster exists. AKS-SRE-Platform
already models this correctly: ADR-013 records a **local kind** platform
runtime (`l10-observability-PASS.md`) precisely because node capacity could not
be measured against Azure.

**Do not conclude an extra node is needed** because pod requests sum below
## 13. Sequential deployment queue

Policy: **one expensive stack at a time.** The persistent low-cost foundation
(`sttfmaia`, Entra identities) may remain.

| # | Project | Preflight | Blocking finding |
|---|---|---|---|
| **1** | AKS-SRE-Platform | ❌ **BLOCKED** | No remote backend; **10/10 vCPU, 0 headroom**; 3 providers unregistered |
| **2** | MAIA | ⚠️ **PARTIAL** | Backend + OIDC ✅ **VERIFIED_LIVE**; minimum live-proof Terraform not written |
| **3** | Enterprise-IT-Helpdesk-Lab | ❌ **BLOCKED** | No remote backend, no OIDC identity; includes ACA env + Postgres |
| **4** | Factory | ❌ **BLOCKED** | No remote backend, no OIDC identity; 4 private endpoints + Postgres |

### Why the queue cannot start yet

Your intended order is right on the merits — AKS-SRE is genuinely the most
quota-sensitive. But **project #1 cannot pass preflight on this subscription
today**, for three independent reasons, only one of which is quota:

1. **No remote state backend.** Its Terraform would write `.tfstate` into the
   working tree. Same for #3 and #4. MAIA is the only repo with a verified
   backend.
2. **Quota has zero headroom.** 10/10, with autoscaling disabled on the user pool.
3. **Providers unregistered** for the managed observability services its own
   architecture depends on.

None of these are permission problems. All are ordinary engineering work that
does not require a quota request.

## 14. Quota decision

```text
Current quota sufficient for sequential validation:  NO
Quota increase required NOW:                        NO — see reasoning
```

**Do not submit a quota request.**

- The cause of the 10-vCPU ceiling is the **Azure for Students free-tier
  default**, not resource exhaustion. Nothing is holding quota: `cores` is
  **0/10 used**.
- Raising it would not unblock project #1, because #1, #3 and #4 are blocked on
  a missing Terraform backend and unregistered providers — none of which are
  quota problems.
- Requesting quota now would be buying headroom for an autoscaling
  demonstration that has not been scoped, on a configuration with no room to
  scale into.

### When a quota request IS justified

Only if, after the backend work, a project is genuinely blocked:

```text
required peak  >  current quota  ->  request smallest sensible headroom
```

For AKS-SRE with an autoscaling proof in scope:

```text
system pool (2 × D4s_v6)          8
user pool initial (1 × D2s_v6)    2
user pool autoscaler peak (3)     6
                                 --
measured peak requirement       14
current quota                   10
requested new total             16   (not 20, not 100)
```

I am **not** submitting this. It becomes justified only once the autoscaling
requirement is confirmed in scope and the backend blocker is cleared.

## 15. Mutations performed

```text
B1 (remote state foundation) — 3 repositories
  3 × state resource group
  3 × storage account (Standard_LRS, shared key disabled, Entra-only)
  3 × tfstate container
  3 × Storage Blob Data Contributor for the operator, at STORAGE ACCOUNT scope
  0 × compute, AKS, ACA, database, Redis, Service Bus, APIM, Front Door
  0 × provider registrations, 0 × quota requests, 0 × deletions
  0 × client secrets, 0 × storage keys used
```

**Compute quota unchanged**: `eastasia cores 0/10`, `southeastasia cores 0/10`,
`lowPriorityCores 0/3`. Storage accounts are not compute-quota consumers.

The portfolio audit (this section's predecessor) was read-only and remains so;
the mutations above are B1 only, and every one is a state-foundation resource.

## 15b. B1 POST-STATE MATRIX

```text
PROJECT      STATE_RG               STORAGE          CONTAINER  SHARED_KEY  PUBLIC_NW  HUMAN_BLOB_SCOPE
MAIA         rg-maia-tfstate        sttfmaia         tfstate    disabled    Allow       storage account
AKS-SRE      rg-aks-tfstate         stakssre         tfstate    disabled    Allow       storage account
Helpdesk     rg-helpdesk-tfstate    sthdhelpdesk     tfstate    disabled    Allow       storage account
Factory      rg-factory-tfstate     stfactorystate   tfstate    disabled    Allow       storage account

PROJECT      STATE_KEY                                      LOCAL_STATE  INIT    REMOTE_STATE        CI_OIDC
MAIA         maia/{dev,validation,prod}                     none         PASS    VERIFIED_LIVE       CONFIGURED
AKS-SRE      aks-sre/validation                             none         PASS    VERIFIED_LIVE       NOT_CONFIGURED
Helpdesk     helpdesk/validation                            none         PASS    VERIFIED_LIVE       NOT_CONFIGURED
Factory      factory/{validation,dev,prod}                  none         PASS    VERIFIED_LIVE       NOT_CONFIGURED
```

`allowSharedKeyAccess` reads back as `null`, which is Azure's representation of
disabled — not an unset value.

Isolation is a control, not a convention: each repository's
`probe_backend_isolation.py` fails if it names another project's state resources,
if two environments share a key, if `use_oidc` is hardcoded, or if a missing
backend config lets `terraform init` fall back to local state.

Public network access is `Allow` on all four accounts, deliberately: GitHub-hosted
runners have no stable outbound IP to allow-list. Recorded as
`PUBLIC_NETWORK_REACHABLE + ENTRA_AUTH_REQUIRED + SHARED_KEY_DISABLED`. None of
these accounts is private-endpoint protected, and none is claimed to be.

## 16. Blockers summary

| # | Blocker | Project | Kind | Would more quota help? |
|---|---|---|---|---|
| B1 | ~~No remote Terraform backend~~ | AKS-SRE, Helpdesk, Factory | engineering | **CLOSED 2026-10-02** — see §15b |
| B2 | 0 vCPU headroom for autoscaling proof | AKS-SRE | capacity | **yes** |
| B3 | `Microsoft.Insights`/`Monitor`/`Dashboard` unregistered | AKS-SRE | registration | no |
| B4 | Minimum live-proof Terraform unwritten (MAIA); required tfvars missing (Helpdesk); `tenant_id` + digest-pinned `container_image` unsupplied (Factory) | MAIA, Helpdesk, Factory | engineering | no |
| B5 | Budget creation rejected by Azure | all | Azure-side | no |

B1 closing revealed B4 in two more repositories than the audit recorded:
Helpdesk has no `environments/*/terraform.tfvars` at all, and Factory's plan stops
on `tenant_id` and `container_image`, which deliberately have no defaults. Both
are application-configuration gaps, not state gaps — the backends themselves are
verified live.

## 17. Next

Do **not** start project #1 preflight yet. In order:

1. **B1** — give AKS-SRE, Helpdesk and Factory the same remote backend shape
   MAIA has (`backend.hcl` per environment, `use_azuread_auth`, no `use_oidc`
   hardcoded, shared state storage, per-project state key). This is copied
   knowledge, not new research, and MAIA's probe rules apply.
2. **B3** — register `Microsoft.Insights`, `Microsoft.Monitor`,
   `Microsoft.Dashboard`. One mutation, explicitly scoped.
3. **B2** — decide whether the autoscaling demonstration is required. If yes, the
   minimum honest request is **16 regional vCPUs**, requested at that point with
   the measured peak stated.
4. **B4** — write MAIA's minimum live-proof Terraform (ACA + Postgres + Redis,
   nothing from the full target).
5. Then, and only then, start **#1 AKS-SRE** preflight.
10 CPU. On a 2 × D4s_v6 system pool, allocatable is already below the 8 raw
vCPU after Kubernetes and system reservations. Layer B must be read from a live
cluster; Layer C only says whether the *workloads* fit.

### Observability profiles

The locked AKS-SRE architecture uses **Managed Prometheus, Managed Grafana,
OpenTelemetry, App Insights / Log Analytics**. ELK is not planned on AKS —
consistent with the provider findings in §7.

These are managed services billed on ingestion/metrics, **not** on node vCPU, so
they do not consume the 10-vCPU ceiling. They do add real cost.

Sequential `PROFILE AUTOSCALER` / `PROFILE OBSERVABILITY` remain defensible, but
with 0 headroom the autoscaler profile is not runnable regardless of
observability, so the sequencing question is currently moot.

### MAIA

| Tier | Resources | Cost class |
|---|---|---|
| Current committed Terraform | RG + UAMI + Entra app + Key Vault + RBAC | low, minutes |
| Minimum live proof | + ACA app, Postgres flexible server, Redis | **hourly** |
| Full target (NOT for validation) | APIM Premium, Front Door Premium, private endpoints, Service Bus | **hourly, high** |

MAIA's committed Terraform has **no** ACA/Postgres/Redis — the committed stack is
identity + vault + RBAC only. The minimum live proof is therefore *unwritten*,
and writing it is a prerequisite, not a deployment.

### Enterprise-IT-Helpdesk-Lab

Committed Terraform (22 `.tf`): VNet + 3 subnets, **ACA environment + ACA app**,
**PostgreSQL flexible server** + database + firewall rules, Service Bus
namespace + queue, Log Analytics workspace, App Insights, Key Vault.

Heaviest of the four by resource count, and it contains an **ACA managed
environment** — the same consumption-billed pattern already running as
`cae-portfolio`.

### Factory

Committed Terraform (47 `.tf`): VNet + 3 subnets, **4 private endpoints** +
private DNS zone, PostgreSQL flexible server, Service Bus namespace + 2 queues,
storage account + container, Log Analytics workspace, Key Vault, Monitor metric
alert + action group.

Private endpoints and managed Postgres are the notable cost and quota consumers.
## 10. Storage security — tfstate

| Property | Value | Verdict |
|---|---|---|
| `allowSharedKeyAccess` | **false** | ✅ no key auth possible |
| `enableHttpsTrafficOnly` | true | ✅ |
| `allowBlobPublicAccess` | false | ✅ |
| `minimumTlsVersion` | TLS1_2 | ✅ |
| `networkRuleSet.defaultAction` | **Allow** | ⚠️ open, required for GitHub-hosted runners |
| private endpoints | 0 | consistent with the above |

The open default action is **deliberate and necessary**: a GitHub-hosted runner
has no stable egress IP to allow-list, so restricting this account would break
CI. The control that matters is that SharedKey is refused, and there is no
shared key to leak.

## 11. Cost inventory

| Item | Value |
|---|---|
| Budgets configured | **0** |
| Measured usage 2026-09-01 → 2026-10-03 | **$0.0000** |
| Services appearing in usage | OperationalInsights, ServiceBus, KeyVault, CDN — all zero quantity |

Forecast pricing is **PRICE_LOOKUP_REQUIRED** — I will not invent numbers. What
*is* measurable is which resources are structurally expensive:

| Resource | Why it costs |
|---|---|
| AKS system pool (2 × D4s_v6 = 8 vCPU) | per-second node billing; largest single line |
| ACA managed environment | per-second consumption while any app runs |
| Log Analytics ingestion | per-GB ingested, plus retention |
| PostgreSQL flexible server | per-hour, even idle |
| Redis | per-hour, even idle |
| APIM / Front Door Premium | per-hour base + per-call |

**Hard cost ceiling: NOT PROVIDED.** Zero budgets exist. `trap destroy` in
`transient_verify.sh` remains the only runtime protection.
identity's Contributor grant. Not stale.