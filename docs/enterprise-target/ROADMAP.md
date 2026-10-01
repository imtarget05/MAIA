# ENTERPRISE TARGET ROADMAP — MAIA

```text
repo     : MAIA
IaC      : Terraform is the canonical Infrastructure-as-Code language
program  : three-repository Enterprise Target (MAIA · Helpdesk · Factory)
authority: this file is the program phase plan. Per-repo state lives in
           CURRENT-STATE.md and COMPLETION-MATRIX.md (same directory).
```

## Execution contract (applies to every phase)

1. Inspect latest `origin/main` before doing anything.
2. Never trust historical SHA / test counts without re-measuring.
3. One writer per repository.
4. Never modify dirty user worktrees.
5. Work from clean temporary worktrees.
6. Never `git add .` / `git add -A` — selective staging only.
7. Never start the next TASK until the current task passes its DoD.
8. Never start the next PHASE until every required task is verified.
9. No claim may exceed evidence.
10. A green exit code alone is not proof of the intended semantic.
11. Mutation evidence counts only if: mutation applied → intended branch executed → intended semantic changed → exact control failed **for the intended reason**.
12. Do not silently fallback in ENTERPRISE mode; local/dev fallbacks only when explicitly selected.
13. No secret values in source, logs, metrics, traces, Terraform outputs, or Terraform state.
14. Do not provision cloud resources unless the current phase explicitly authorizes Azure mutation.
15. Expensive Azure resources may be transient: apply → verify → evidence → destroy.
16. Never confuse source SHA / OCI digest / Terraform state / Azure revision / runtime behavior.
17. Keep all three repositories independently deployable.
18. No shared Terraform module dependencies between repos.

**Stop only for:** missing credentials · missing Azure/GitHub privileges · destructive irreversible action · unexpected paid-resource cost · concurrent writer conflict · architecture contradiction not resolvable from source/evidence. Normal failing tests are NOT blockers — fix them.

## Phases

| Phase | Name | Exit gate (must be 100%) |
|---|---|---|
| 0 | Canonical Reconciliation | 3 exact `origin/main` SHAs · clean worktrees · no conflicting writers · tests measured · CI measured · Azure inventory measured · live revision measured |
| 1 | Terraform Migration | per-repo Bicep↔Terraform parity · no cross-repo modules · `terraform test` green · plan-JSON controls bite · Bicep frozen · Azure unchanged |
| 2 | Terraform State + GitHub OIDC | remote state · locking · OIDC works · wrong subject fails · no `AZURE_CLIENT_SECRET` · exact issuer/audience/subject read back |
| 3 | Import Existing Azure | `terraform plan` shows no unexpected destroy / replacement · prod-critical `prevent_destroy` |
| 4 | Network + Identity + Secrets | private DNS resolves · public path denied · UAMI works · missing RBAC → Forbidden · no secret in state |
| 5 | Data + Messaging + Workers | duplicate → one semantic result · crash → retry · repeated failure → DLQ · DLQ replay → deterministic recovery |
| 6 | Distributed Application Correctness | survives replica switching + restart (see COMPLETION-MATRIX) |
| **6A** | **AI Reliability + Guardrails** | provider timeout/retry/fallback + injection + tenant isolation (see AI-PRODUCTION-GATE.md) |
| **6B** | **Multi-Agent Safety** | agent justification · durable checkpoint/resume · loop bounds · least-privilege tools · HITL |
| **6C** | **AI Performance + Cost** | bounded concurrency · timeout budget · true SSE/TTFT · model routing · cost metrics · cache isolation |
| **6D** | **AI Evaluation + Regression Gate** | golden dataset · retrieval+generation metrics · slice-level regression gate · calibrated judge |
| **6E** | **RAG Production Hardening** | retrieval ACL · tenant isolation · hybrid + reranker actually execute · empty/no-answer behavior · chunk provenance · Recall@K / Precision@K / MRR / NDCG · retrieval regression gate |
| **6F** | **MCP Tool Platform** | typed MCP tools (schema + authz + idempotency) · read/write separated · high-risk write behind HITL · tool output untrusted · negative controls |
| **6G** | **Agent + RAG + MCP E2E** | full business workflow, positive + negative E2E · failure injection at each boundary |
| 7 | Observability + SRE | synthetic incident: Grafana symptom → trace → logs → root cause → alert → runbook → recovery |
| 8 | Edge + WAF + APIM | normal 200 · WAF blocks · direct backend restricted · APIM 429 policy · correlation ID end-to-end |
| 9 | CI/CD + Provenance + Rollout | OIDC-only · immutable OCI · SBOM + scan · digest pin · candidate→10%→90%→100% · rollback proven |
| 10 | Failure Injection + DR + Security | every dependency: detection · bounded retry · no unsafe fallback · telemetry · recovery; measured RTO/RPO |
| 11 | FinOps + Full Enterprise Validation | full env from Terraform · 3 E2E workflows · machine-readable evidence · destroy · no orphan paid resources |
| 12 | Evidence + FINAL FREEZE | re-run from clean clones · reconcile SHA/digest/state/revision/runtime · two diagrams · no over-claim |

## Phase 6A–6G is mandatory

Phases **6A–6G may not be skipped** because unit tests pass or the app runs. They sit between application correctness (6) and observability (7).

- **6A–6D** — control spec, gates, invariants, paste-ready master prompt + AI DoD: [`AI-PRODUCTION-GATE.md`](./AI-PRODUCTION-GATE.md)
- **6E–6G** — RAG hardening, MCP tool platform, Agent+RAG+MCP E2E: [`MCP-RAG-AGENT.md`](./MCP-RAG-AGENT.md)

## Definition of "done" (whole program)

```text
SOURCE → Terraform → plan-json controls → Azure → runtime
       → failure injection → metrics/logs/traces → business E2E → evidence
```

Every link must agree. A green CI is not the definition of done.

## Terminal statuses (Phase 12 only)

```text
VERIFIED_LIVE · VERIFIED_TRANSIENT · IMPLEMENTED_TESTED · N/A_WITH_EVIDENCE
```

No `PARTIAL` / `PLANNED_ONLY` / `NOT_VERIFIED` for required scope. Final verdict per repo is binary: **FINAL FROZEN** or **NOT DONE** — no partial freeze.
