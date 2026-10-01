# MAIA enterprise infrastructure (`infra/`)

V1 scope: **security foundation, validation only — nothing here deploys
production in TODO 3.** The Bicep stack is reviewed, builds clean, and is
gated in CI. Deployment, migration and runtime verification belong to later
waves with their own evidence.

Two entrypoints, split deliberately (not feature flags — a flag can be
flipped by accident, a missing module cannot be deployed):

- `main.bicep` — **V1 ONLY**: resource groups, managed identity, Key Vault,
  RBAC. Enforced by `infra/scripts/validate-v1-scope.py` (CI-gated).
- `main.v6-target.bicep` — full target architecture (edge, APIM, ACR,
  observability) for later waves. Parameters: `v6-dev` / `v6-prod`.
- `parameters/v1-dev|prod.bicepparam` — V1 parameters for `main.bicep`.

## Layout

```text
infra/
├── main.bicep                 # subscription-scoped wiring (params only, no resources)
├── resourceGroups.bicep       # 3 RGs: identity / edge / apps (blast-radius boundary)
├── bicepconfig.json           # Graph extension (pinned), core analyzers on
├── parameters/
│   ├── dev.bicepparam         # dev names/tags; example contact; zero-GUID deploy principal
│   └── prod.bicepparam        # prod names/tags; same contract
└── modules/
    ├── identity/              # user-assigned MI + Entra app (no interactive sign-in)
    ├── keyvault/              # RBAC model, purge protection, NO secret values
    ├── rbac/                  # least-privilege grants, one invocation per RG
    ├── apps/                  # ACR + Container Apps env + app (Key Vault secret refs)
    ├── observability/         # Log Analytics + App Insights + diagnostics
    ├── apim/                  # ⚪ out of V1 deploy scope (build-validated only)
    ├── edge/                  # ⚪ out of V1 deploy scope (Front Door Premium has real cost)
    └── apim-policies/         # gateway policy XML, referenced by apim modules
```

## Required parameters (before any real deployment)

Set in `parameters/{dev,prod}.bicepparam`: `ownerContact` (monitored mailbox),
globally-unique `keyVaultName` / `containerRegistryName`, tenant-unique
`entraApplicationName`, non-empty redirect URIs, and a real
`deployIdentityPrincipalId` (the committed zero GUID fails what-if loudly by
design). Secret VALUES are never committed — the vault deploys empty and
values are written out of band (see future deployment runbook).

## Validation (no Azure deployment)

```bash
az bicep build --file infra/main.bicep            # must exit 0, zero diagnostics
az bicep build --file infra/parameters/dev.bicepparam   # expands against main.bicep
/tmp/gitleaks detect --no-git --source infra/     # must report no leaks
```

Future (NOT in TODO 3): `az deployment sub what-if` with a real
`deployIdentityPrincipalId`, then a scoped V1-only deployment. `main.bicep`
currently always includes edge/apim — a V1 deploy wave must gate or split
that scope first, and must decide greenfield RGs vs adopting
`rg-portfolio-evidence`/`cae-portfolio`/`ca-maia-api` (recorded limitation,
not a TODO 3 defect).

## Cost

Managed Identity and Bicep do not introduce a direct service charge; Key Vault
is usage-priced and expected to have low portfolio-scale cost. Actual cost has
not yet been measured. Front Door Premium, APIM and Premium ACR in this
template carry real cost and are NOT approved for deployment by this file.

## Rollback

TODO 3 deploys nothing: rollback = revert the IaC/CI commits. For future
deployments, rollback keeps the prior app configuration/revision and restores
traffic — never "delete the deployment" as primary strategy, and the old
secret/env path stays until the identity path is verified.
