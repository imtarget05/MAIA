# MAIA — Terraform (canonical IaC target, Phase 1)
#
# STATUS: MIGRATION_CANDIDATE. Bicep (`infra/*.bicep`) remains
# CURRENT_CANONICAL_IAC until this reaches verified source parity and is merged.
# No `terraform apply` happens during Phase 1.
#
# ## What this directory is
#
# A faithful Terraform port of the **V1 deployment unit** (`infra/main.bicep`):
# identity resource group + managed identity / Entra registration + Key Vault
# + RBAC. Everything else in Bicep (APIM / edge / apps / observability,
# `main.v6-target.bicep`) is target-only and is classified — not ported — in
# `docs/enterprise-target/TERRAFORM-MIGRATION-MAP.md`.
#
# ## Layout
#
# ```text
# infra/terraform/
# ├── versions.tf     provider pins (azurerm, azuread)
# ├── providers.tf    provider config (no credentials here, ever)
# ├── backend.tf      remote-state contract (commented out until Phase 2)
# ├── variables.tf    input contract (mirrors main.bicep params)
# ├── locals.tf       shared tags + well-known role definition ids
# ├── main.tf         root composition (RG + identity + keyvault + rbac)
# ├── outputs.tf      parity with main.bicep outputs
# ├── modules/        repo-local child modules (no cross-repo references)
# ├── environments/   per-environment *.tfvars (+ backend.hcl in Phase 2)
# ├── tests/          *.tftest.hcl + check_plan_invariants.py
# └── policies/       policy/scans inputs (Trivy config)
# ```
#
# ## Run
#
# ```bash
# cd infra/terraform
# terraform init                      # providers only; local state (no Azure)
# terraform fmt -check
# terraform validate
# terraform test
# terraform plan -var-file=environments/prod/terraform.tfvars -out=tfplan
# terraform show -json tfplan > plan.json
# python3 tests/check_plan_invariants.py plan.json
# ```
#
# ## Rules
#
# - Never prove infrastructure by grepping HCL. The semantic proof is the
#   compiled plan (`plan.json`) read by `check_plan_invariants.py`.
# - No secret VALUES in Terraform (source, variables, outputs, state).
# - No cross-repo module sources (`git::`, registry, symlinks to another repo).
# - `.terraform/`, `*.tfstate`, `*.tfplan`, `plan.json`: never committed.
