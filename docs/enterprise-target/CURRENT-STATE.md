# CURRENT STATE — MAIA

```text
measured_at : 2026-10-01
method      : read-only (git + filesystem). Azure NOT probed. Test suites NOT run.
mutations   : `git fetch --all --prune` only (remote-tracking refs; working tree untouched)
rule        : no number appears below unless it was measured here, or is explicitly
              labelled CARRIED_FORWARD_NOT_REMEASURED.
```

## 1. Identity

| Field | Value |
|---|---|
| remote | `https://github.com/imtarget05/MAIA.git` |
| **canonical ref (`origin/main`)** | `c9e356bb8cd8316f1d84980f4b7da5440f29c90e` (Render-cleanup merge, PR #13) |
| local `HEAD` | not canonical — see drift row |
| drift vs origin | local branches are stale/unpushed (the Phase 1A Terraform port lives on `migration/terraform-maia`); `origin/main` is the only quotable revision |
| Render purge | **DONE** — `render.yaml`, `.github/workflows/cd.yml`, `.github/workflows/keepalive.yml` and the `ci.yml` `deploy-production` job removed in PR #13 (merge `c9e356bb`); anti-Render gate `tests/test_hygiene_no_render_deployment.py` (4 tests) merged; `origin/main` tree has **zero** `render.yaml`/`keepalive*` artifacts |
| worktree | **CLEAN** |
| latest tag | `maia-closeout-verified` |

**Canonical SHA is `c9e356bb`.** The previously recorded `ee306496` is superseded by the Render-cleanup merge; unpushed local drift must never be quoted as the deployed/verified revision.

## 2. Infrastructure as deployed today

| Field | Value |
|---|---|
| IaC language | **Bicep** at this snapshot; the Terraform port has since landed in `infra/terraform/` — see §7 |
| entrypoints | `infra/main.bicep`, `infra/main.v6-target.bicep`, `infra/resourceGroups.bicep` |
| modules | `infra/modules/{apim,apps,edge,identity,keyvault,observability,rbac}` |
| policies | `infra/apim-policies/` |
| parameters | `infra/parameters/{dev,prod,v1-dev,v1-prod,v6-dev,v6-prod}.bicepparam` |
| invariant checker | `infra/check_invariants.py` (asserts on **compiled ARM JSON**, not `.bicep` source) |
| validation | `infra/validate.sh`, `infra/bicepconfig.json` |
| CI | `.github/workflows/iac-validate.yml` *(also: `ci.yml`, `ci-live.yml`, `build-container.yml`, `llm-gateway.yml`, `threshold-calibration.yml`)* — Render `cd.yml`/`keepalive.yml` removed in repository cleanup |
| edge / APIM | present as Bicep modules (`apim`, `edge`) |

## 3. Verified seams present in source

> Presence in source ≠ verified at runtime. This section records existence only.

- Hybrid retrieval (dense + BM25, RRF k=60) — `src/maia/retriever.py::HybridRetriever`
- Cross-encoder reranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`) — `src/maia/reranker.py`
- SSE token streaming (typed contract: meta → token → citations → done) — `src/maia/api.py` (`POST /chat/stream`) + `src/maia/streaming.py`
- HITL approval (LangGraph interrupt + durable checkpoint) — `src/maia/agent/langgraph_agent.py`

## 4. Open defects (carried, with source)

- **[maia-gate8-unmeasured]** P1 — abstention gate **exits 1** (authorised 8 of 9 labelled no-answer queries); refusal-accuracy is **UNMEASURED** (usable = 0 of 9). *source: `docs/PORTFOLIO-COMPLETION-AUDIT-v2.md`*
- **[maia-iac-invariant-contract-drift]** P1 — *CLOSED on both halves; the entry is kept for traceability.* It read: the Bicep ARM checker crashed on the canonical ARM output and never reached a verdict, and the Terraform checker had to be built to fail closed.
  - **Bicep half — closed 2026-10-02** (`docs/evidence/bicep-invariants/`): `infra/validate.sh` reached a real verdict (`2 invariant(s) held across 1 vault(s)`) and the traversal harness held 22/22 contracts. The checker has since been **deleted** along with the Bicep stack (PR #17), after proving no workflow, validation script, Terraform gate or test invoked it. The lessons it encoded are preserved in `docs/evidence/bicep-invariants/README.md` and implemented in the Terraform successors.
  - **Terraform half — closed 2026-10-02**: the plan-JSON checker fails closed with a JSON path on an unreadable, unparseable or non-object plan, and on uninspectable change shapes.
- **[maia-runtime-image-deps]** — `requirements.api.txt` **deliberately excludes** `torch`, `sentence-transformers`, `fastembed`, `rank-bm25`, `onnxruntime` (Container Apps **Consumption = 1 GiB** → OOM). *source: `requirements.api.txt` (in-repo).* → "reranker in the runtime image" is an **ACA sizing decision**, not a config toggle.

## 5. NOT YET MEASURED (fail-closed)

- Azure live revision / image digest .... **UNMEASURED**
- cost exposure ......................... **UNMEASURED**

MEASURED (CI, `@ c9e356bb`, run `36914855138`, 20m33s — all jobs green):

- test suite @ `origin/main` ............ **1167 passed / 14 skipped / 3 xfailed** (2 deselected)
  — the pre-cleanup baseline was 1163 passed; the +4 are the anti-Render hygiene gate.
- CI status @ `origin/main` ............. **GREEN**, 13 jobs (SonarCloud advisory skipped)

CARRIED_FORWARD_NOT_REMEASURED (from audit docs only — do NOT quote as verified): `998 passed`; live revision `ca-maia-api--0000006`; unresolved two-revision identities.

## 6. Hazards

- `$HOME` (`/Users/mainguyenbinhtan`) is a **DIRTY worktree** of `FlashSale-Backend` (branch `interview-release/auth`). `Projects/.git` is an **empty stub** → any git run from `Projects/` resolves to `$HOME`. **All git MUST use `git -C <abs repo path>`.**
- Local branches are stale/unpushed (the Phase 1A Terraform port lives on `migration/terraform-maia`) → never treat local `HEAD` as canonical; always `git fetch` and quote `origin/main`.

## 7. Addendum — Phase 1 Terraform migration (measured 2026-10-02)

> This addendum is a separate measurement from §1–§6 above, which stay as their
> 2026-10-01 snapshot. Nothing below is carried forward from another document;
> every number is printed by `docs/evidence/terraform-phase1/generate.sh`.

```text
source_sha : 4303dd6d000dcf25e309cc61336e3c3a08477d91
branch     : migration/terraform-maia
scope      : offline only — no Azure login, no plan against a subscription, no apply
tools      : terraform 1.16.3 · trivy 0.74.0 · python 3.14.7
```

| Field | Value |
|---|---|
| Terraform as source | 19 `*.tf` under `infra/terraform` (gitignored `.terraform/` excluded) |
| Contracts | 4 `*.tftest.hcl` (root composition + 3 modules), 2 python test files, 1 plan fixture |
| `terraform test` | root **2**, identity **2**, keyvault **1**, rbac **2** — all green |
| Plan-JSON controls | 7 invariants PASS on the committed fixture **and** on a live-captured `plan.json` |
| Control negative controls | `probe_plan_controls.py`: **15 passed, 0 failed** (11 mutations + 3 read-error + 1 baseline) |
| Policy scan | PASS; 1 documented exception `AZU-0013`; positive control confirms the scanner detects a weakened vault |
| Bicep | **0** `.bicep` / `.bicepparam` changed vs `origin/main` |
| Cross-repo modules | none — all 3 module sources are `./modules/…` |
| CI | `.github/workflows/terraform-validate.yml` runs the gate with `permissions: contents:read`, no Azure auth |
| Evidence | `docs/evidence/terraform-phase1/2026-10-02-gate.log` (regenerable) |

### What was wrong before this measurement, and is now fixed

Recorded because "it passes" says nothing about what changed:

| Before (parent commit `f815a958`) | Now |
|---|---|
| root `terraform test` → `Success! 0 passed, 0 failed` | root asserts the resource group, tag contract and role-id wiring |
| `terraform test` in all 3 modules → `Error: unknown provider …/azurerm` | 5 module assertions execute (2 / 1 / 2) |
| module `init` resolved azurerm **5.7.0** against a root pin of `~> 4.0` | modules pin via `versions.tf` + their own lock files |
| plan checker → **traceback** on an unreadable plan | fail-closed verdict line, never a traceback |
| no CI ran Terraform at all | `terraform-validate.yml`, paths-filtered on `infra/terraform/**` |

### Still NOT verified — do not read this addendum as Phase 1 complete

- **No Azure anything**: no login, no plan against a subscription, no apply, no
  import. Nothing here is evidence that a resource exists.
- **Bicep is still `CURRENT_CANONICAL_IAC`.** The port is a candidate on a
  branch with no upstream; it is not merged, so it is not canonical.
- **`azure/login` was never run**, so `CURRENT-STATE` §5 stays as it was: test
  suite @ `origin/main`, CI @ `origin/main`, live Azure revision, image digest
  and cost exposure are all still **UNMEASURED**.
- `infra/check_invariants.py` (Bicep) was **not** re-run in this pass.
