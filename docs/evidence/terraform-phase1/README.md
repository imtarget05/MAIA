# Phase 1 (Terraform migration) evidence

## What is here

| File | What it records |
|---|---|
| `generate.sh` | The committed generator: re-run it to regenerate the log below |
| `2026-10-02-gate.log` | One run of `infra/terraform/scripts/phase1_check.sh` (offline) at a frozen SHA, plus the plan-JSON negative controls, the policy scan and the fail-closed check |

Regenerate with the committed generator, so a future run replaces this file
rather than contradicting it:

```bash
./docs/evidence/terraform-phase1/generate.sh      # writes <today>-gate.log
```

Every number in the log is printed by a command the script runs — nothing is
typed by hand — so re-running it either reproduces the file or shows exactly
which figure moved. The underlying gate itself is:

```bash
cd infra/terraform
./scripts/phase1_check.sh          # fmt · init · validate · test · probe · scan
python3 tests/probe_plan_controls.py   # control negative controls
./policies/scan.sh                     # policy scan + positive control
```

## What this evidence does and does not establish

**Establishes** (all measured at the SHA named in the log header):

- the offline gate passes: `fmt -check`, `init -backend=false`, `validate`,
  `terraform test` for the root and all three modules, the plan-JSON negative
  controls, and the Trivy policy scan;
- `terraform test` actually executed assertions (the log prints the per-target
  counts, because an empty run and a passing run share an exit code);
- each plan invariant fails for its own reason when mutated (11 mutations), and
  an unreadable plan produces a verdict line rather than a traceback;
- Bicep is untouched on the branch (0 changed `.bicep` / `.bicepparam`), and
  every Terraform module source is repo-local.

**Does not establish** — stated here so the pass is not over-read:

- that any Azure resource exists. No `az login`, no `terraform plan` against a
  subscription and no `terraform apply` ran; this is source parity only.
- that Terraform is the canonical IaC yet. Bicep remains
  `CURRENT_CANONICAL_IAC` until the port is merged.
- anything about remote state, OIDC or import. Those are Phase 2 and Phase 3.

## Related

- Phase plan: [`../../enterprise-target/ROADMAP.md`](../../enterprise-target/ROADMAP.md)
- Per-repo state: [`../../enterprise-target/CURRENT-STATE.md`](../../enterprise-target/CURRENT-STATE.md)
- Parity classification: [`../../enterprise-target/TERRAFORM-MIGRATION-MAP.md`](../../enterprise-target/TERRAFORM-MIGRATION-MAP.md)
- Directory contract: [`../../../infra/terraform/README.md`](../../../infra/terraform/README.md)
- Policy scan rationale: [`../../../infra/terraform/policies/README.md`](../../../infra/terraform/policies/README.md)