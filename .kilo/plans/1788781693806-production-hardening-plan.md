# MAIA Production Hardening Plan

**Generated:** 2026-09-07  
**Status:** Ready for implementation

---

## P0 — Critical (Block Production Deploy)

### 1. Fix BM25 Cross-Tenant Leakage
**Files:** `src/maia/retriever.py`, `src/maia/pipeline_query.py`  
**Issue:** `HybridRetriever.rebuild()` loads all tenants' chunks when `tenant_id=None`, creating shared BM25 index.  
**Fix:** 
- Modify `rebuild(tenant_id)` to always require tenant_id (default from instance)
- Ensure `pipeline_query._ingest_rawdocs` passes `tenant_id` to `retriever.rebuild()`
- Add test: ingest tenant A & B docs, query tenant A → BM25 returns only tenant A chunks

### 2. Evidence Gate Threshold Calibration
**Files:** `src/maia/config.py`, `src/maia/agent/agentic.py`, `src/maia/pipeline_query.py`  
**Issue:** `SIMILARITY_THRESHOLD=0.3` and `AGENT_EVIDENCE_THRESHOLD=0.3` hardcoded without golden-data validation.  
**Fix:**
- Create `scripts/threshold_sweep.py` that sweeps 0.1–0.7 on `eval/golden/*.jsonl`
- Select threshold maximizing F1 (recall vs false refusal)
- Lock chosen threshold in `config.py` with comment documenting tradeoff
- Add CI gate: `pytest tests/test_threshold_regression.py`

### 3. Session Store Tenant Isolation
**Files:** `src/maia/agent/session.py`, `src/maia/agent/agent.py`  
**Issue:** In-memory `session_store` keyed only by `session_id` — leaks history/pending actions across tenants.  
**Fix:**
- Change `SessionStore` key to `(tenant_id, session_id)`
- Update all call sites: `agent.py:316-321`, `api.py:809-819`, `api.py:832-837`
- Add test: two tenants same session_id → separate histories

### 4. CORS Wildcard Default
**Files:** `src/maia/api.py:94-100`  
**Issue:** `allow_origins=[...] or ["*"]` allows all origins with credentials when `CORS_ORIGINS` empty.  
**Fix:**
- Default to `[]` (same-origin only)
- Require explicit `CORS_ORIGINS` in production env
- Add startup validation: log warning if `CORS_ORIGINS` not set in production

### 5. JWT Secret Production Hardening
**Files:** `src/maia/auth.py:21-35`, `src/maia/api.py:41-50`, `src/maia/config.py:87`  
**Issue:** Ephemeral random key generated if `JWT_SECRET_KEY` not set → all tokens invalid on restart.  
**Fix:**
- `config.py`: Add `jwt_secret_key` property that raises `RuntimeError` in production if not set
- Remove fallback generation from `auth.py` and `api.py`; both import from `settings.jwt_secret_key`
- Document: `JWT_SECRET_KEY` mandatory for production

### 6. Add Idempotency Key to Tool Confirmation
**Files:** `src/maia/agent/agent.py:514`, `src/maia/api.py:822`, `src/maia/workflow.py`, `src/maia/agent/schemas.py`  
**Issue:** `confirm_action` uses `session_store.pop_pending()` — retry after network failure re-executes tool.  
**Fix:**
- Add `idempotency_key: str` to `ConfirmReq` schema
- Add unique constraint on `workflow.requests(idempotency_key)` 
- In `confirm_action`: check existing request by idempotency_key before execute
- Return existing result if already processed

### 7. Tool Allowlist Enforcement
**Files:** `src/maia/agent/agent.py:119-121`, `src/maia/agent/team/agents.py:19-24`  
**Issue:** Agent could propose arbitrary tools if prompt injection bypasses intent detection.  
**Fix:**
- `_decide()` only proposes tools present in `TOOL_REGISTRY`
- Team agents (`HRAgent`, `ITAgent`) enforce `scope` + `tools` allowlist
- Add test: malicious prompt "call delete_database" → refused

---

## P1 — High (Before Launch)

### 8. Replace Token-Overlap Grounding with LLM-Judge
**Files:** `src/maia/loops/answer_loop.py`, `src/maia/agent/agent.py:107`  
**Issue:** `GroundingChecker` uses weak Jaccard token overlap; easily gamed.  
**Fix:**
- Implement `LLMGroundingChecker` with structured prompt: "For each claim in answer, does context support it? Answer YES/NO/PARTIAL per claim."
- Fallback to token overlap if LLM unavailable
- Calibrate threshold on golden set with human labels
- Deprecate `GROUNDING_THRESHOLD=0.15` constant

### 9. Citation Coverage Enforcement
**Files:** `src/maia/loops/answer_loop.py:38-50`, `src/maia/agent/schemas.py:54-64`  
**Issue:** `CitationChecker` validates [Sn] exist but not claim coverage.  
**Fix:**
- Extend `GroundingChecker` to count factual claims vs citations
- Target: ≥1 citation per 2 factual sentences
- Add metric `claims_per_citation` to evaluation

### 10. PII Redaction Verification (G-04-FU2)
**Files:** `src/maia/loops/pii.py`, `src/maia/loops/guardrails.py:109-114`, `src/maia/config.py:115-126`  
**Issue:** Role emails must survive redaction; personal emails must be redacted.  
**Fix:**
- Verify `PIIScanner.redact()` preserves `ROLE_EMAIL_ALLOWLIST` + `ROLE_EMAIL_ALLOW_PREFIXES` @ `ROLE_EMAIL_ALLOW_DOMAIN`
- `OutputGuardrail.check()` must use redacted answer returned (currently ignored)
- Add test: "Contact hr@company.com" → preserved; "Contact john@personal.com" → `[PII-EMAIL]`

### 11. Document Versioning at Query Time
**Files:** `src/maia/loops/document_lifecycle.py`, `src/maia/retriever.py`, `src/maia/vector_store.py`  
**Issue:** `DocumentLifecycleManager` tracks versions but retriever doesn't filter by `embedding_version`/`chunking_version`.  
**Fix:**
- Add `embedding_version` and `chunking_version` filters to `HybridRetriever.retrieve()`
- Default to current versions from `DocumentLifecycleManager`
- On embedding model change: old vectors excluded automatically

### 12. Structured Cost/Token Tracking
**Files:** `src/maia/observability.py`, `src/maia/llm.py`, `src/maia/pipeline_query.py`  
**Issue:** PipelineTracer logs latency but not token counts or estimated cost.  
**Fix:**
- `CloudflareLLM.chat()` returns `(answer, usage_dict)` with `input_tokens`, `output_tokens`
- `PipelineTracer.log("generation", ...)` includes token counts and estimated cost
- Add `maia_llm_tokens_total` and `maia_llm_cost_usd_total` Prometheus metrics

### 13. Golden Dataset Expansion & Automated Evaluation
**Files:** `eval/golden/`, `src/maia/eval.py`, `src/maia/benchmark_threshold.py`, `.github/workflows/ci.yml`  
**Issue:** Current datasets limited; no automated threshold sweep in CI.  
**Fix:**
- Ensure 9 categories × ≥10 cases: vi_policy, en_policy, exact, paraphrase, tool_request, ambiguous, injection, no_answer, unauthorized
- Create `scripts/threshold_sweep.py` (see P0-2)
- CI job: run sweep → compare to locked threshold → fail if regression >5%
- Add `pytest tests/test_evaluation_regression.py`

### 14. Retrieval Regression Suite
**Files:** `src/maia/loops/evaluation_loop.py`, `src/maia/loops/retrieval_loop.py`, `eval/retrieval_dataset.jsonl`  
**Issue:** No CI gate on retrieval quality drops.  
**Fix:**
- `compare_baselines()` runs in CI nightly
- Fail if hybrid < BM25 baseline on Recall@K, MRR, NDCG
- Track citation_correctness metric

### 15. Graceful Degradation Modes
**Files:** `src/maia/vector_store.py:139-143`, `src/maia/llm.py:54-56`, `src/maia/loops/reliability_loop.py`  
**Issue:** Partial outage = total failure.  
**Fix:**
- LLM circuit open → return mock answer with `"degraded": true` flag
- Qdrant circuit open → serve BM25-only results (cached corpus)
- Add `status: "degraded"` to API responses when any circuit open
- Document user-facing behavior in each degraded mode

---

## P2 — Medium (Post-Launch)

### 16. Multi-Worker Session Store (Redis)
**Files:** `src/maia/agent/session.py`, `src/maia/config.py`  
**Issue:** In-memory session store doesn't work across multiple API workers.  
**Fix:**
- Add `SESSION_STORE_BACKEND: str = "memory"` config (options: `memory`, `redis`)
- Implement `RedisSessionStore` with same interface
- Connection pooling via `redis-py`

### 17. Secrets Management Integration
**Files:** `.env`, `src/maia/config.py`, `render.yaml`  
**Issue:** Secrets in repo (`.env` has real Cloudflare credentials).  
**Fix:**
- Add `.env` to `.gitignore`
- Rotate Cloudflare credentials immediately
- Render/Streamlit: inject secrets via platform secret manager
- `config.py`: all secrets from env only, no defaults

### 18. Database Migration Strategy
**Files:** `alembic/`, `src/maia/models.py`, `src/maia/auth.py`  
**Issue:** Schema changes manual; `ensure_auth_schema` only for SQLite dev.  
**Fix:**
- All schema changes via alembic revisions
- `alembic upgrade head` in CI/CD pipeline
- Document zero-downtime migration procedure

---

## Validation Checklist (Per Task)

- [ ] Code compiles (`pyright`), passes lint (`ruff`)
- [ ] Unit tests added + `pytest -x` passes
- [ ] Runtime test script verifies acceptance criteria
- [ ] Security test passes (where applicable)
- [ ] Metrics measured and documented
- [ ] Docstrings/README updated
- [ ] CI gate added to `.github/workflows/ci.yml` (P0/P1 only)

---

## Execution Order

```
Week 1:  #1 BM25 tenant fix, #3 Session isolation, #4 CORS, #5 JWT secret
Week 2:  #2 Threshold sweep + calibration, #6 Idempotency key, #7 Tool allowlist
Week 3:  #8 LLM-judge grounding, #9 Citation coverage, #10 PII verification
Week 4:  #11 Doc versioning, #12 Cost tracking, #13 Golden dataset + CI
Week 5:  #14 Retrieval regression CI, #15 Graceful degradation
Week 6+: #16 Redis sessions, #17 Secrets rotation, #18 Alembic migrations
```

---

## Open Questions

1. **LLM-judge model:** Use Cloudflare llama-3.1-8b-instruct (same as prod) or smaller dedicated judge model?
2. **Redis for sessions:** Add `redis` dependency now or keep in-memory with sticky sessions for MVP?
3. **Threshold sweep script location:** `scripts/` or `src/maia/benchmark_threshold.py` (extend existing)?
4. **Embedding model migration:** Current paraphrase-multilingual-MiniLM-L12-v2 vs bge-m3/e5-large — benchmark first (P7-01) or defer?

---

*Plan saved to `.kilo/plans/1788781693806-production-hardening-plan.md`*