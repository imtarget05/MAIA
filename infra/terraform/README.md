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
# ├── versions.tf       provider pins (azurerm, azuread)
# ├── providers.tf      provider config (no credentials here, ever)
# ├── backend.tf        remote-state contract (commented out until Phase 2)
# ├── variables.tf      input contract (mirrors main.bicep params)
# ├── locals.tf         shared tags + well-known role definition ids
# ├── main.tf           root composition (RG + identity + keyvault + rbac)
# ├── outputs.tf        parity with main.bicep outputs
# ├── modules/          repo-local child modules (each with its own
# │   │                   versions.tf and tests/contract.tftest.hcl)
# ├── environments/     per-environment *.tfvars (+ backend.hcl in Phase 2)
# ├── tests/            composition.tftest.hcl, check_plan_invariants.py,
# │                       probe_plan_controls.py, fixtures/plan.v1.json
# ├── scripts/          phase1_check.sh — the whole offline gate (CI runs it)
# └── policies/         Trivy config + reviewed ignore list + scan.sh
# ```
#
# ## Run
#
# One entry point. It runs everything below and fails on the first failure, so
# no step can be quietly skipped — a skipped step prints exactly what a passing
# one prints.
#
# ```bash
# cd infra/terraform
# ./scripts/phase1_check.sh              # offline: fmt · init · validate ·
#                                        #   test (root + modules) · plan-JSON
#                                        #   negative controls · Trivy policy scan
# ./scripts/phase1_check.sh --plan prod  # + a real plan + the invariant checker.
#                                        #   Needs Azure credentials. CI never runs it.
# ```
#
# The individual steps, for when you want one of them alone:
#
# ```bash
# terraform init -backend=false          # providers only; local state (no Azure)
# terraform fmt -check -recursive
# terraform validate
# terraform test                         # also: cd modules/<name> && terraform test
# python3 tests/probe_plan_controls.py   # does the invariant checker actually bite?
# ./policies/scan.sh                     # positive control + repo scan + ignore set
# terraform plan -var-file=environments/prod/terraform.tfvars -out=tfplan
# terraform show -json tfplan > plan.json
# python3 tests/check_plan_invariants.py plan.json
# ```
#
# `terraform test` in a child module needs `terraform init -backend=false` run
# inside that module first: a module has no parent to inherit `required_providers`
# from, and without them the run fails with `unknown provider` rather than running
# the assertions. Each module therefore declares its own `versions.tf`.
#
# ## Rules
#
# - Never prove infrastructure by grepping HCL. The semantic proof is the
#   compiled plan (`plan.json`) read by `check_plan_invariants.py`.
# - A control that cannot run is a FAILURE, not a skip. An empty `terraform test`
#   (`0 passed, 0 failed`) and a red one both exit 0 — so `phase1_check.sh`
#   asserts the counts rather than trusting the exit code.
# - An unreadable plan fails closed with a verdict line, never a traceback: a
#   caller reading only the exit code cannot tell a crash from a finding.
# - No secret VALUES in Terraform (source, variables, outputs, state).
# - No cross-repo module sources (`git::`, registry, symlinks to another repo).
# - `.terraform/`, `*.tfstate`, `*.tfplan`, `tfplan`, `plan.json`: never committed.
#
