# TERRAFORM MIGRATION MAP — MAIA (TF-M1, living until parity is VERIFIED)
#
# Every existing Bicep source is exactly one of:
#   PARITY_VERIFIED            ported, invariants hold
#   INTENTIONALLY_NOT_PORTED   consciously excluded, with a reason
#   TARGET_ONLY_NOT_CURRENT    exists in source; was never canonical runtime IaC
#   SUPERSEDED                 replaced by a newer canonical source
#
# No unexplained omission is allowed. Do NOT infer Azure deployment existence
# from Bicep — this table describes SOURCE parity.

## Canonical V1 deployment unit (infra/main.bicep, subscription scope)

| Bicep source | Resource type | Current purpose | Terraform module | Terraform resource | Parity status | Notes |
|---|---|---|---|---|---|---|
| `main.bicep` · identity resource group | `Microsoft.Resources/resourceGroups` | holds identity + Entra app + Key Vault (`rg-maia-identity`) | root | `azurerm_resource_group.identity` | **PARITY_VERIFIED** | target-only edge/apps RGs NOT created by V1 |
| `modules/identity` · UAMI | `Microsoft.ManagedIdentity/userAssignedIdentities` | workload runtime identity (`id-maia`) | `modules/identity` | `azurerm_user_assigned_identity.workload` | **PARITY_VERIFIED** | |
| `modules/identity` · application | `Microsoft.Graph/applications` | Entra registration (MyOrg, v2 tokens, no interactive users, no consent) | `modules/identity` | `azuread_application_registration.workload` | **PARITY_VERIFIED** | `azuread` provider required; `type` enum is adapter casing only |
| `modules/identity` · service principal | `Microsoft.Graph/servicePrincipals` | SP backing the registration | `modules/identity` | `azuread_service_principal.workload` | **PARITY_VERIFIED** | |
| `modules/identity` · web/spa redirect URIs | Graph platform auth config | empty in V1 (no gateway serves them) | `modules/identity` | `azuread_application_redirect_uris.web/.spa` | **PARITY_VERIFIED** | empty lists; no dangling trust |
| `modules/keyvault` · vault | `Microsoft.KeyVault/vaults` | runtime secrets (`kv-maia-01`); RBAC-only; purge protection; soft-delete 90d; public endpoint open in V1 | `modules/keyvault` | `azurerm_key_vault.vault` | **PARITY_VERIFIED** | KNOWN MAP-LEVEL DRIFT: `managedBy` tag bicep->terraform (expected on import) |
| `modules/rbac` · Key Vault Secrets User | `Microsoft.Authorization/roleAssignments` | app resolves its own secrets (never rotates) | `modules/rbac` | `azurerm_role_assignment.key_vault_secrets_user` | **PARITY_VERIFIED** | conditional on vault id; Secrets User, not Officer |
| `modules/rbac` · deploy Contributor | `Microsoft.Authorization/roleAssignments` | CI identity can roll back this scope only | `modules/rbac` | `azurerm_role_assignment.deploy_identity_contributor` | **PARITY_VERIFIED** | scope is the identity RG id, never subscription |
| `modules/rbac` · AcrPull | `Microsoft.Authorization/roleAssignments` | container pull grant | `modules/rbac` | `azurerm_role_assignment.acr_pull` | **PARITY_VERIFIED** | suppressed when `container_registry_id = null` (V1) |
| `modules/rbac` · Blob Data Contributor | `Microsoft.Authorization/roleAssignments` | future storage access (V4 extension point) | `modules/rbac` | `azurerm_role_assignment.blob_data_contributor` | **PARITY_VERIFIED** | suppressed when `blob_storage_account_id = null` (V1) |

## v6-target design record (infra/main.v6-target.bicep + siblings)

| Bicep source | Resource type | Current purpose | Parity status | Notes |
|---|---|---|---|---|
| `infra/main.v6-target.bicep` | full-stack entrypoint | design record, NEVER deployed (see its header) | **TARGET_ONLY_NOT_CURRENT** | port module-by-module only when the needing phase arrives |
| `infra/resourceGroups.bicep` · edge/apps RGs | `Microsoft.Resources/resourceGroups` | future RG boundary | **TARGET_ONLY_NOT_CURRENT** | V1 main.bicep creates the identity RG alone |
| `infra/modules/apps/` | ACA env + app + probes | future runtime | **TARGET_ONLY_NOT_CURRENT** | ported in TF-M5 when ACA source contract becomes canonical scope |
| `infra/modules/apim/` + `infra/apim-policies/` | APIM surface + policies | future edge | **TARGET_ONLY_NOT_CURRENT** | TF-M8 |
| `infra/modules/edge/` | Front Door + WAF | future edge | **TARGET_ONLY_NOT_CURRENT** | TF-M8 |
| `infra/modules/observability/` | Log Analytics + App Insights | future observability | **TARGET_ONLY_NOT_CURRENT** | TF-M7 |
| `infra/validate/negative-secret/` | negative-test fixture | CI guard, not a deployment | **INTENTIONALLY_NOT_PORTED** | covered by invariant checker behaviour, not by an Azure resource |

## What Terraform parity deliberately does NOT include

- PostgreSQL / Redis / Storage / Service Bus / Event Grid: **no** such resource
  exists in current MAIA Bicep; inventing them now would violate the parity rule
  ("port what Bicep has, not what the roadmap wants").
- Remote state, federated credentials, live import, `plan` against Azure:
  Phase 2 / Phase 3 scope, explicitly out of TF-M2..M15 except the exact
  subtasks that name them.

## Terraform-only artefacts (no Bicep counterpart)

The tables above classify **Bicep sources**. The files below have no Bicep
counterpart and therefore cannot appear in those tables — recording them here
means the directory has no unclassified content, which is the rule this document
exists to enforce.

| Artefact | Purpose | Status |
|---|---|---|
| `infra/terraform/tests/composition.tftest.hcl` | Root wiring contract: identity RG name/location, the shared tag contract, and the four role ids forwarded from `locals.tf` into the rbac module | **ADDED_BY_MIGRATION** |
| `infra/terraform/modules/*/versions.tf` | Provider requirements per module, so `terraform test` inside a module can resolve providers and stays on the root's majors | **ADDED_BY_MIGRATION** |
| `infra/terraform/tests/probe_plan_controls.py` | Negative controls: 11 mutations against the committed plan fixture, asserting each invariant fails for its own reason | **ADDED_BY_MIGRATION** |
| `infra/terraform/tests/fixtures/plan.v1.json` | Reduced, placeholder-only capture of a real V1 plan, so CI can run the invariant checker without authenticating to Azure | **ADDED_BY_MIGRATION** |
| `infra/terraform/scripts/phase1_check.sh` | The single offline gate entry point; asserts test **counts**, because an empty `terraform test` and a passing one share an exit code | **ADDED_BY_MIGRATION** |
| `infra/terraform/policies/` | Trivy config, one reviewed exception (`AZU-0013`) with its removal condition, and a scan whose positive control must fire before a clean result is trusted | **ADDED_BY_MIGRATION** |
| `.github/workflows/terraform-validate.yml` | Runs the gate in CI with `contents: read` and no Azure authentication | **ADDED_BY_MIGRATION** |

### Why these are not `INTENTIONALLY_NOT_PORTED`

They are verification apparatus for the port itself, not deployment units. Bicep
has no equivalent because Bicep has no equivalent requirement: the parity test
is against **the Bicep source**, and these files are what makes that test
reproducible by anyone with a clone.

## Re-verification note

The `PARITY_VERIFIED` rows above were re-read **source-to-source** on 2026-10-02
(`4303dd6d`) — every Bicep parameter, resource property, output and conditional
guard against its Terraform counterpart. That is a source-parity check and is
nothing more: no Azure resource was planned, applied, imported or queried in that
pass, so **no row here asserts that a resource exists**. Evidence:
`docs/evidence/terraform-phase1/2026-10-02-gate.log`.
