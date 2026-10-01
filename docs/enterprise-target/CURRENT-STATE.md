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
| IaC language | **Bicep** (Terraform: **NOT PRESENT** — 0 `*.tf` files) |
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
- **[maia-iac-invariant-contract-drift]** P1 — `infra/check_invariants.py` **crashes** on the canonical ARM output; it cannot distinguish an invariant violation from an unsupported ARM node, so it **never reached a verdict**. *source: same.* → Terraform `check_plan_invariants.py` must fail closed with a JSON path, never a traceback.
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
