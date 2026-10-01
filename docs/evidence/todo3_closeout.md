# MAIA TODO 3 closeout — V1 security foundation (identity, Key Vault, RBAC)

Date (UTC): 2026-10-01
Canonical main: `0e4f50a` — `Merge pull request #3 from imtarget05/reconcile/v1-gates`
Remediation commit (retained in history): `4271b55` — `ci: run the secret scan on a pinned CLI and gate V1 security properties`
Scope chain: `ee5c938` (V1/V6 split + scope invariant) → `4271b55` → `0e4f50a`
Canonical CI: run `36842382944` — **SUCCESS, 14/14 jobs green**
Azure deployment: **NONE**. No Azure resource was created, modified or deleted
during TODO 3.

Two P1 incidents were raised and closed during this wave. Both are recorded
below with their history intact, because the reasoning matters more than the
outcome: in both cases the first reading of the symptom was wrong, and the
useful part is what the evidence forced us to change our minds about.

---

## 1. P1 — IaC invariant-checker contract drift

**Status: RESOLVED / VERIFIED**

### Historical failure

The compiled-ARM invariant checker built during this wave crashed against the
newer ARM resource shape produced by the V1 entrypoint, before it could return
a Key Vault invariant verdict at all.

### What this was NOT

This was **not** evidence that the Key Vault configuration violated the
invariants. `enablePurgeProtection: true` and `enableRbacAuthorization: true`
were correct in the module on `main` throughout, and remain so. The gap was in
the observer, not in the observed. The correct reading is "we could not tell",
which is a different claim from "it was wrong", and it is recorded that way
here so the distinction is not lost.

### Root cause

The old recursive walker assumed every entry in a nested ARM `resources` list
was a resource object. Compiling the current V1 entrypoint against
`infra/modules/identity` produced a nested `resources` collection containing
**bare strings** — the symbolic names of the enclosing module's variables and
outputs — alongside the real resource dicts. A resource whose properties are
computed rather than literal additionally serialises the whole `properties` bag
as an ARM expression string. The walker called `.get()` on both and raised
`AttributeError`.

A security check that crashes is a security check that gets switched off, so the
crash was the defect, not a cosmetic annoyance.

### Remediation

`infra/check_invariants.py` was rebuilt against the current compiled ARM
structure rather than adapted to fit an old one:

- skips entries that are not resource objects instead of raising;
- treats `properties` as optional and non-dict-safe;
- records the JSON path of whatever it inspects, so a failure names the exact
  node (`$.resources[2].properties.template.resources[0]`) rather than nothing;
- **fails closed** — a vault it cannot locate is a FAIL that lists the paths it
  searched, never a skip and never a pass;
- compares value *and* type, so a mis-serialised truthy `"false"` string cannot
  satisfy a boolean `True` assertion.

The template was not modified to suit the checker. That direction would have
made the test pass by changing the thing under test.

### Verified controls

Every row was executed against the current compiled V1 ARM, not reasoned about.

| Control | Result |
|---|---|
| clean compiled V1 ARM | **PASS** |
| `enablePurgeProtection: false` | **FAIL**, with JSON path |
| `enableRbacAuthorization: false` | **FAIL**, with JSON path |
| vault absent from a real compile | **FAIL**, lists searched paths |
| bare strings mixed into `resources` | handled, no crash |
| `properties` is an ARM expression string | **FAIL**, values unreadable |
| property is the string `"false"` | **FAIL**, not coerced to `True` |
| property absent entirely | **FAIL**, reported as absent |
| `resources` is not a list | **FAIL**, no crash |
| top-level template is a list | **FAIL**, no crash |

Zero silent skips and zero tracebacks across the tested failure paths.

---


## 2. P1 — nondeterministic Gitleaks pull-request gate

**Status: RESOLVED / VERIFIED**

### Historical failure

The secret-scanning job went red on a pull request with *"missing gitleaks
license"* on a repository containing no secret.

### Root cause

`gitleaks-action@v2` decides whether a license is required by asking the GitHub
API who owns the repository. On a **push** run that lookup succeeded —
*"[user] is an individual user. No license key is required"* — and the job was
green. On the **pull_request** run the same lookup hit an API rate limit, so the
action could not classify the account, fell back to *"License key validation
will be enforced"*, and failed.

Same repository, same revision, same config, opposite verdict, decided by a
transient API response. The failure mode is the real problem: a scanner that
turns red on a rate limit teaches people to re-run it, and re-running until
green is how a genuine finding gets waved through.

Provisioning a `GITLEAKS_LICENSE` secret was rejected. It would have hidden the
symptom while leaving the non-deterministic dependency in place, which is the
worse of the two failures.

### Remediation

- pinned Gitleaks CLI **8.28.0**, run directly;
- the release's **published SHA256** verified with `sha256sum -c -` before the
  binary is executed (`a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb`,
  verified against the published asset);
- no `GITLEAKS_LICENSE` dependency;
- no `gitleaks-action@v2` owner-classification dependency;
- **current-tree scan is blocking** (exit 1 on a finding);
- **historical scan is advisory** (exit 0, reports and uploads a report).

The two scans have different contracts on purpose. The tree gate is
deterministic and is what a new commit can break. Gating on history would mean
failing every build until someone rewrites history.

### Verification

| Case | Result |
|---|---|
| current working tree | no leaks |
| fake AWS access key + fake GitHub PAT in an isolated dir | **detected** (2 findings) |
| intended OCI full-SHA image tag in `infra/parameters/` | suppressed |
| the same OCI string in any other path | **detected** |

The allowlist is a true `path AND regex` intersection, re-verified after the
parameter files were renamed to the `v1-`/`v6-` split, so it neither fails open
nor masks a credential committed elsewhere.

### Historical advisory finding

One finding remains detectable in git history: a test canary in
`tests/test_tracing.py` that named a fake value `SECRET-XYZ-123`, introduced in
`54b87d8`. It is **not a credential compromise** and is not recorded as one. It
is a placeholder in a test whose purpose is to assert the value never appears in
a span attribute. It is fixed in the current tree (renamed `pii_canary`, at
source rather than allowlisted), and **no history rewrite was performed**,
because rewriting would invalidate every recorded SHA, CI run and audit
reference this repository depends on.

A green working tree therefore does **not** mean repository history is clean.

## 3. V1 deployment-scope evidence

`infra/main.bicep` is the V1-only entrypoint; `infra/main.v6-target.bicep` is
the full end-state wiring, compiled and linted but not deployed.

| Check | Result |
|---|---|
| V1 scope invariant | **22 resource checks** |
| later-wave resource types provisionable under `v1-dev` / `v1-prod` | **0** |
| APIM injected into V1 | gate **FAIL** |
| storage grant enabled through parameters | gate **FAIL** |

V1 **includes**: identity, Key Vault, RBAC, and the intended resource-group
boundaries.

V1 **excludes deployment of**: APIM, Front Door, ACR, PostgreSQL, Redis, Azure
AI Search, and application runtime resources.

The V1 deployment unit creates one resource group. The deploy identity receives
Contributor on that group alone: a subscription-scoped Contributor can attach
role assignments to itself and escalate to Owner, a resource-group-scoped one
cannot, and that claim is checkable with a single `az role assignment list`.

---

## 4. Tenant identifier

A real directory (tenant) id had been committed in the IaC parameter files, in
files whose own header claimed *"no secret values, ever"*. To be precise: a
tenant id is **not a credential** and grants no access on its own. It is an
**environment-specific identifier committed by accident** on a public remote.

The current tree is clean; the parameter files use the zero-GUID placeholder and
`infra/validate.sh` now fails the build on any non-placeholder tenant or
principal id. The identifier **remains in git history** and is not rewritten
away, for the reason given in §2: it is not a secret, and a rewrite would
invalidate the evidence chain this document depends on.

---

## 5. Superseded operator worktree

Not part of the canonical repository state, recorded only so the evidence chain
is complete: a separate worktree of this repository holds three IaC commits
(`0012650`, `66ced3a`, `5a26c3d`) that were **superseded** by `ee5c938` and are
not on `main`'s canonical line. They are not evidence for anything on this page;
every claim above is measured on `0e4f50a`.

---

## 6. Final verdict

```
TODO 3 — V1 SECURITY FOUNDATION = VERIFIED DONE
```

Canonical chain:

```
0e4f50a  Merge pull request #3 from imtarget05/reconcile/v1-gates
  -> GitHub Actions run 36842382944
  -> 14/14 jobs SUCCESS
```

Per-gate confirmation read from the canonical run log, not inferred from the
workflow's exit status:

| Gate | Evidence in run `36842382944` |
|---|---|
| Bicep validate | job success |
| V1 scope invariant | `22 resource checks, 0 later-wave types provisionable` |
| purge/RBAC invariant | `enablePurgeProtection == True`, `enableRbacAuthorization == True`, `2 invariant(s) held across 1 vault(s)` |
| Gitleaks pinned CLI | `gitleaks.tar.gz: OK`, `no leaks found`, `historical findings: 1` (advisory) |
| Full workflow | **14/14 jobs green, no non-green job** |

**No Azure resource was created, modified or deleted during TODO 3.** Every
claim on this page is about templates, gates and repository state.

It is not, and the workflow emits a warning annotation on every run so that
stays visible rather than being rediscovered later.

---
