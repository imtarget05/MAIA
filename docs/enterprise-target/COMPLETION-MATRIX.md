# COMPLETION MATRIX — MAIA

```text
derived_from : docs/enterprise-target/CURRENT-STATE.md
measured_at  : 2026-10-01
updated_at   : 2026-10-02 — row 1 re-measured at source_sha 4303dd6d
               (docs/evidence/terraform-phase1/2026-10-02-gate.log). Every
               other row below is unchanged from the 2026-10-01 pass and is
               NOT re-measured here.
```

## Status vocabulary

```text
VERIFIED_LIVE       measured on a live Azure runtime; evidence retained
VERIFIED_TRANSIENT  measured in a transient env that was then destroyed; evidence retained
IMPLEMENTED_TESTED  code + tests exist AND a green suite was observed at a frozen SHA
N/A_WITH_EVIDENCE   outside required scope, with a written reason

INTERIM (must be resolved before Phase 12 freeze — never a terminal state):

IMPLEMENTED_UNVERIFIED  source + tests exist; suite NOT re-run this pass; runtime not verified
UNMEASURED              required; not yet measured in this program
NOT_PASSING             a measured control currently fails
NOT_PRESENT             required; does not exist yet
```

No required row may be closed with `PARTIAL` / `PLANNED_ONLY` / `NOT_VERIFIED`.

## Matrix

| # | Required component | Status (now) | Evidence measured | Close in |
|---|---|---|---|---|
| 1 | Terraform as canonical IaC | **IMPLEMENTED_TESTED** (not yet canonical) | at `4303dd6d`: 19 `*.tf`, 4 `*.tftest.hcl`; root+modules `terraform test` green; 7 plan invariants PASS; 15 control negative controls; Bicep **0** files changed; no Azure plan/apply/import. **NOT_PRESENT no longer applies**, but Bicep is still `CURRENT_CANONICAL_IAC` until the port is merged | Phase 1 (merge = canonical) |
| 2 | Remote state + GitHub OIDC (no `AZURE_CLIENT_SECRET`) | **VERIFIED_LIVE** | **Bootstrap + local backend (measured with az):** `sttfmaia` with `allowSharedKeyAccess=false`, HTTPS-only, TLS1_2; `tfstate` container listable via Entra with no account key; app `maia-github-oidc` has `passwordCredentials=0` and `keyCredentials=0`; GitHub SP holds exactly 3 assignments (Contributor @ `rg-maia-verify`; Reader + Blob Data Contributor @ `rg-maia-tfstate`), no Owner anywhere; local `terraform init` against the azurerm backend + `state pull` with no local `.tfstate`. **GitHub Actions OIDC (run 36983504649, workflow green):** `oidc-login` succeeded via `azure/login` with no client secret and `az account show` returned sub `a3deec78-7edb-41cd-9e94-ec1d4d9379f5` / tenant `aa79a92c-ec09-4de1-baa9-151b8f9df886`; `oidc-negative-control` presented subject `…:ref:refs/heads/main` and Azure rejected it with **AADSTS700213** quoting that exact subject, and the job stayed GREEN on that expected rejection; `terraform-plan` job initialised the azurerm remote backend through OIDC (`ARM_USE_OIDC`/`ARM_USE_AZUREAD_AUTH`, no secret), confirmed no local state file, `state pull` reachable, plan **9 add / 0 change / 0 destroy** with all 7 plan invariants PASS (DRIFT0 create-only allowlist). `rg-maia-verify` still holds **zero** resources. **NOT MEASURED**: any `terraform apply` of the application stack | Phase 2 |
| 3 | Import existing Azure resources (no recreate) | **UNMEASURED** | live inventory not probed | Phase 3 |
| 4 | VNet + Private Endpoints + Private DNS | **UNMEASURED** | `infra/modules/*` network shape only | Phase 4 |
| 5 | UAMI + Key Vault (secret VALUES outside TF state) | **UNMEASURED** | `infra/modules/identity`, `keyvault` | Phase 4 |
| 6 | PostgreSQL (tenants / conversation / LangGraph checkpoints / HITL) | **UNMEASURED** | no PG module in MAIA Bicep today | Phase 5 |
| 7 | Redis (global rate limit / cache / idempotency TTL) | **UNMEASURED** | — | Phase 5–6 |
| 8 | Blob → Event Grid → Service Bus → ingestion worker | **UNMEASURED** | — | Phase 5 |
| 9 | Qdrant Cloud | **UNMEASURED** | `qdrant-client` in requirements | Phase 5 |
| 10 | Hybrid search (dense + BM25 + RRF) | **IMPLEMENTED_UNVERIFIED** | `src/maia/retriever.py`; `tests/test_loops.py` | Phase 6 |
| 11 | Reranker present in runtime image | **UNMEASURED** (blocked: ACA sizing) | `src/maia/reranker.py` exists; `requirements.api.txt` excludes it | Phase 1/6 |
| 12 | SSE token streaming | **IMPLEMENTED_UNVERIFIED** | `src/maia/api.py` `POST /chat/stream`; `tests/test_chat_stream_sse.py` | Phase 6 |
| 13 | Abstention / evidence gate | **NOT_PASSING** | gate exits 1; usable 0/9 | Phase 6 |
| 14 | Multi-replica (2+) correctness | **UNMEASURED** | — | Phase 6 |
| 15 | OTel + App Insights + Log Analytics + Prometheus + Grafana + SLO | **UNMEASURED** | `observability/`, `observability/slo.yaml` | Phase 7 |
| 16 | Front Door Premium + WAF + APIM | **UNMEASURED** | `infra/modules/edge`, `apim`; `infra/apim-policies/` | Phase 8 |
| 17 | OCI build + SBOM + digest-pinned rollout | **UNMEASURED** | `build-container.yml` (Render `cd.yml` removed) | Phase 9 |

> Deployed claims carried from audit docs (`998 passed`, rev `ca-maia-api--0000006`, two unresolved revision identities) are **CARRIED_FORWARD_NOT_REMEASURED** and must not be quoted as verified until re-measured.

## AI Production Readiness (Phase 6A–6D)

> Mandatory gate between Phase 6 and Phase 7. Full spec: [`AI-PRODUCTION-GATE.md`](./AI-PRODUCTION-GATE.md).

| # | Required component | Status (now) | Evidence measured | Close in |
|---|---|---|---|---|
| 18 | 6A.1 Provider timeout / retry / fallback / circuit breaker | **UNMEASURED** | `llm.py` has retry; cross-provider matrix not verified | 6A |
| 19 | 6A.2 Side-effect idempotency | **UNMEASURED** | — | 6A |
| 20 | 6A.3 Structured AI output validation (schema + business + authz) | **IMPLEMENTED_UNVERIFIED** | `src/maia/pipeline/nl2sql.py` (guard + allowlist) | 6A |
| 21 | 6A.4 Direct prompt-injection controls | **IMPLEMENTED_UNVERIFIED** | `tests/test_prompt_injection.py` | 6A |
| 22 | 6A.5 RAG tenant isolation | **IMPLEMENTED_UNVERIFIED** | `src/maia/vector_store.py` tenant filter; runtime negative control not run | 6A |
| 23 | 6A.6 No secrets in prompt / log / metric / trace | **UNMEASURED** | — | 6A |
| 24 | 6B.1 Agent justification gate | **UNMEASURED** | — | 6B |
| 25 | 6B.2 Durable checkpoint / resume (PostgreSQL) | **IMPLEMENTED_TESTED** | `get_durable_graph()` uses PostgresSaver; two real OS processes proven by `tests/test_served_path_durability.py`. Azure multi-replica NOT verified | 6B |
| 26 | 6B.3 Loop protection (steps / tool calls / tokens / cost) | **UNMEASURED** | — | 6B |
| 27 | 6B.4 Least-privilege tools + HITL | **IMPLEMENTED_UNVERIFIED** | `langgraph_agent.py::node_propose_action` + interrupt | 6B |
| 28 | 6B.5 Indirect injection (tool / document) | **IMPLEMENTED_UNVERIFIED** | `tests/test_prompt_injection.py` | 6B |
| 29 | 6C.1 Bounded concurrency + load test | **UNMEASURED** | — | 6C |
| 30 | 6C.3 True SSE + TTFT | **IMPLEMENTED_UNVERIFIED** | `api.py::/chat/stream`, `streaming.py`; provider-TTFT not verified | 6C |
| 31 | 6C.5 AI cost metrics | **UNMEASURED** | — | 6C |
| 32 | 6C.6 Cache isolation | **UNMEASURED** | — | 6C |
| 33 | 6D.1 Golden dataset | **IMPLEMENTED_UNVERIFIED** | `eval/dataset.jsonl`, `eval/retrieval_dataset.jsonl` | 6D |
| 34 | 6D.2 Retrieval metrics per stage | **IMPLEMENTED_UNVERIFIED** | `gate8b_rag_quality.py` | 6D |
| 35 | 6D.3 Generation / abstention metrics | **NOT_PASSING** | gate exits 1; usable 0/9 | 6D |
| 36 | 6D.4 Slice-level regression gate in CI | **UNMEASURED** | `threshold-calibration.yml` exists | 6D |
| 37 | 6D.6 LLM-as-judge calibration | **IMPLEMENTED_UNVERIFIED** | `src/maia/eval_judge.py` | 6D |
| 38 | Phase 7 AI distributed tracing | **UNMEASURED** | `observability/` | 7 |

## MCP + RAG + Agent (Phase 6E–6G)

> Full spec: [`MCP-RAG-AGENT.md`](./MCP-RAG-AGENT.md). MAIA identity: **Production Agentic RAG + MCP AI Platform**.

| # | Required component | Status (now) | Evidence measured | Close in |
|---|---|---|---|---|
| 39 | 6E Retrieval ACL + tenant isolation | **IMPLEMENTED_UNVERIFIED** | `src/maia/vector_store.py` tenant filter; runtime negative control not run | 6E |
| 40 | 6E Hybrid + reranker actually execute in runtime | **UNMEASURED** | reranker absent from API image (`requirements.api.txt`) | 6E |
| 41 | 6E Empty-retrieval / no-answer behavior | **NOT_PASSING** | gate exits 1; usable 0/9 | 6E |
| 42 | 6E Chunk provenance + citation mapping | **IMPLEMENTED_UNVERIFIED** | `[S1]` citation projection; live probe returned `citations([])` | 6E |
| 43 | 6E Retrieval metrics (Recall@K / Precision@K / MRR / NDCG) | **IMPLEMENTED_UNVERIFIED** | `gate8b_rag_quality.py` | 6E |
| 44 | 6F MCP tool platform (schemas + authz + idempotency) | **IMPLEMENTED_UNVERIFIED** | `src/maia/mcp/servers/` (4 servers) | 6F |
| 45 | 6F Read/write separation; no shell / raw-SQL tool | **UNMEASURED** | — | 6F |
| 46 | 6F MCP negative controls | **UNMEASURED** | — | 6F |
| 47 | 6G Agent + RAG + MCP end-to-end | **UNMEASURED** | — | 6G |
