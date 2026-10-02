# MAIA enterprise infrastructure (`infra/`)

**Terraform is the only IaC language in this repository.** Every `.bicep` and
`.bicepparam` file has been deleted; `infra/terraform/` is the sole source of
truth. Nothing here deploys production — `validate.sh` runs `terraform init
-backend=false`, `validate` and `test`, and never `plan` or `apply` against a
subscription.

## Layout

```text
infra/
├── validate.sh                   # THE IaC gate. Fail-closed, Terraform-only.
├── terraform/                    # ← the infrastructure source of truth
│   ├── main.tf                   # resource group + module wiring
│   ├── variables.tf / locals.tf / outputs.tf
│   ├── backend.tf                # intentionally empty until Phase 2
│   ├── environments/{dev,validation,prod}/terraform.tfvars
│   ├── modules/
│   │   ├── identity/             # user-assigned MI (no interactive sign-in)
│   │   ├── keyvault/             # RBAC model, purge protection, NO secret values
│   │   └── rbac/                 # least-privilege grants (Key Vault Secrets USER)
│   └── tests/                    # contract tests + plan-invariant controls
├── terraform/
└── scripts/                      # historical Bicep-era tooling
```

## The Bicep invariant checker is gone

`check_invariants.py`, `scripts/test_checker_traversal.py` and
`scripts/validate-v1-scope.py` were **deleted** after verifying that no workflow,
no validation script, no Terraform gate and no test invoked them — only
documentation did.

That README previously said the files were "retained as-is per the cleanup
decision" and that "its 22 traversal contracts still run". Nothing ran them. A
security checker that no gate executes reads as protection in review while
providing none, so the dead code was removed rather than left looking active.

The lessons those contracts encoded — assert a *specific* discovery at a
*specific* path, treat a missing resource as failure rather than skip, fail
closed with a JSON path instead of a traceback, check value **and** type, and
keep a negative control that proves the harness can fail — are preserved in
[`docs/evidence/bicep-invariants/README.md`](../../docs/evidence/bicep-invariants/README.md)
and are all implemented in the Terraform successors:
`infra/terraform/tests/check_plan_invariants.py` and
`infra/terraform/tests/probe_plan_controls.py`.

### Modules not yet ported

The deleted Bicep stack also carried `apps` (ACR + Container Apps),
`observability`, `apim`, `edge` and `apim-policies`. **Those are NOT yet
present in Terraform.** MAIA's Terraform currently covers the identity +
key vault + RBAC foundation only. Do not read the Terraform tree as a complete
port of the deleted Bicep tree.

## Required inputs (before any real deployment)

`environments/*/terraform.tfvars`: `ownerContact` (monitored mailbox),
globally-unique `key_vault_name` / `container_registry_name`, tenant id, and a
real deploy identity principal id. Secret **values** are never committed — the
vault is created empty by design and values are written out of band.

## Validation (no Azure deployment, no remote state)

```bash
./infra/validate.sh    # fmt · init -backend=false · validate · test · controls · secret scan
```

`validate.sh` refuses to report PASS when there is no `*.tf` under
`infra/terraform`, so the gate cannot go green by having nothing to check.

## Cost

Managed Identity and Terraform do not introduce a direct service charge; Key
Vault is usage-priced. Actual cost has not yet been measured. Front Door
Premium, APIM and Premium ACR carry real cost and are NOT approved for
deployment by this file.

## Rollback

TODO 3 deploys nothing: rollback = revert the IaC/CI commits. For future
deployments, rollback keeps the prior app configuration/revision and restores
traffic — never "delete the deployment" as primary strategy, and the old
secret/env path stays until the identity path is verified.
