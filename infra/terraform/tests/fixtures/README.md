# Plan fixture — provenance and sanitisation

`plan.v1.json` is the known-good plan document the negative-control probe
(`../probe_plan_controls.py`) mutates.

## Why a committed fixture exists

`check_plan_invariants.py` reads a COMPILED plan. Producing one requires
`terraform plan`, which requires Azure credentials — and CI for this repository
deliberately never authenticates to Azure (see `.github/workflows/terraform-validate.yml`).
Without a committed fixture the invariant checker could not run in CI at all,
and "plan-JSON controls bite" would remain an unenforced claim.

## How it was produced

It is a **reduced extract** of a real `plan.json` generated from
`infra/terraform` with `environments/prod/terraform.tfvars`:

```bash
cd infra/terraform
terraform plan -var-file=environments/prod/terraform.tfvars -out=tfplan
terraform show -json tfplan > plan.json
```

Only three top-level keys are kept — `format_version`, `terraform_version` and
`resource_changes` — because `resource_changes` is the only key the checker
reads. Keeping the real shape (rather than a hand-written stub) means the fixture
cannot silently diverge from what Terraform actually emits.

## Why it is safe to commit

The V1 plan contains **no secret values and no real identifiers**: every value is
a placeholder from `environments/prod/terraform.tfvars`
(`00000000-0000-0000-0000-000000000000`, `platform-team@example.invalid`,
`kv-maia-01`). Terraform state and real variable files are never committed —
see `../.gitignore`.

## Maintenance

When the V1 resource set changes, regenerate the fixture from a fresh plan. Do
**not** hand-edit it: a hand-edited fixture stops being evidence.
