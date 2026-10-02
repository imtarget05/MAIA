# Bicep invariant checker — removed, lessons preserved here

```text
removed_by : this commit
reason     : PR #17 deleted the Bicep stack, leaving the checker with no input
what it was: infra/check_invariants.py  (+ infra/scripts/test_checker_traversal.py,
             + infra/scripts/validate-v1-scope.py)
```

## Why the files were deleted rather than kept

They were not accidentally orphaned, and the proof was run before removing
anything:

| Check | Result |
|---|---|
| any active GitHub workflow invokes them | **no** |
| `infra/validate.sh` (now Terraform-only) invokes them | **no** |
| the current Terraform gate depends on them | **no** |
| the pytest suite exercises them | **no** |
| remaining references | documentation only |

`infra/README.md` recorded that the retention was deliberate — "retained as-is
per the cleanup decision… its 22 traversal contracts still run". That last clause
was the problem: nothing ran them. A security checker that no gate executes is
worse than no checker, because it reads as protection in review while providing
none. A deleted checker cannot be mistaken for a live one.

## The lessons, which are the parts worth keeping

The 22 traversal contracts the harness asserted are the real content, and they
transfer directly to the Terraform side. What they taught:

1. **A walker can pass by never entering the branch it claims to check.** Every
   positive contract asserted that a *specific* vault was discovered at a
   *specific* JSON path — not merely that the run exited 0.
2. **A missing resource is a FAILURE, never a skip.** "The vault is not in the
   template" means the guarantee cannot be checked; reporting that as a pass is
   the exact failure the checker exists to prevent.
3. **Unreadable structure must fail closed with a path, not a traceback.** A
   caller reading only the exit code cannot distinguish a crash from a finding.
4. **An unsupported shape is not an absent one.** `Unsupported` was raised, not
   skipped, so "the walk refused to guess" is a visible outcome.
5. **Value AND type.** A mis-serialised truthy `"false"` string must not satisfy
   a boolean invariant.
6. **Paths must be rooted and propagated.** `resources[2].list[0]` beats
   "something nested failed".
7. **A negative control is mandatory.** Two of the contracts broke the input on
   purpose to prove the harness could fail.

Every one of these is implemented in the Terraform successor:
`infra/terraform/tests/check_plan_invariants.py` (fail-closed reads, shape
hardening, named verdicts) and `infra/terraform/tests/probe_plan_controls.py`
(15 negative controls, each asserted to fail for its own reason).

## What is NOT implied by this removal

Deleting a Bicep-era checker says nothing about whether the live Azure estate
matches the Terraform source. That remains:

```text
TERRAFORM_SOURCE_CANONICAL = YES
LIVE_AZURE_TERRAFORM_PARITY = UNVERIFIED
                          = BLOCKED_ON_AZURE_PROVIDER_AUTH
```

No `terraform plan` against a real subscription, no import, no apply has run.
Deleting Bicep before live import verification is a recorded process deviation,
not evidence of parity.
