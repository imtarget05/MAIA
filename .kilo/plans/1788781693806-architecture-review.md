# MAIA Architecture Review

## 1. System Map (Text Diagram)

### Bounded Contexts & Module Responsibilities

| Context | Module(s) | Responsibility |
|---------|-----------|----------------|
| **Indexing Pipeline** | `ingestion.py`, `chunking.py`, `embeddings.py`, `vector_store.py`, `pipeline_query.py:_ingest_rawdocs` | Parse → clean → chunk → embed → upsert to Qdrant with sanitization (G-03) + PII redaction (G-04) |
| **Query Pipeline** | `retriever.py`, `reranker.py`, `prompt.py`, `llm.py`, `pipeline_query.py:query` | Hybrid dense+BM25 → RRF → rerank → context assembly → LLLM → citations |
| **Agentic RAG** | `agent/agent.py`, `agent/agentic.py`, `agent/intents.py`, `agent/tools.py`, `agent/session.py`, `agent/memory.py` | Decide→Retrieve/Memory/Tool→Evidence→Grounding, 10 intents, slots, C1 approval gate |
| **Multi-Agent Teams** | `agent/teams.py`, `agent/team/orchestrator.py`, `agent/team/agents.py`, `agent/team/base.py` | Router→Delegation (Knowledge/HR/IT scoped), hop guard, timeout, team_trace |
| **Streaming Ingestion** | `stream/producer.py`, `stream/worker.py`, `stream/transport.py`, `stream/broker_kafka.py`, `stream/store.py`, `stream/events.py`, `stream/metrics.py` | Kafka chunk events → consumer group → parallel workers → idempotent upsert → commit-after-upsert → DLQ |
| **Reliability/Observability** | `loops/reliability_loop.py`, `loops/resilience.py`, `stream/metrics.py` | Circuit breakers, retry/backoff, lag metrics, autoscaling policy, health report |
| **Guardrails/Safety** | `loops/guardrails.py`, `loops/pii.py`, `loops/corrective_rag.py`, `loops/document_lifecycle.py` | Input/output guardrails, injection sanitization, PII redaction, CRAG, versioning |
| **Auth/Identity** | `auth.py`, `models.py`, `api.py` (auth router) | JWT (access/refresh), password reset, employee_id allocation, RBAC via tenant_id |
| **External Connectors** | `mcp/client.py`, `mcp/servers.py` | GitHub/Notion read-only search (token-gated, never raise) |
| **Voice/Finetune** | `voice/handler.py`, `voice/providers.py`, `finetune/export.py`, `finetune/train.py` | STT/TTS protocols, triplet export, GPU train runner (all off by default) |

### Data & Control Flow (Main Paths)

**Query Path (single-turn RAG):**
```
POST /query → api.py:do_query → pipeline_query.query()
  → build_stack() [creates embedder, store, retriever, reranker, llm]
  → retriever.retrieve() [dense Qdrant + BM25 → RRF]
  → reranker.rerank() [cross-encoder or score fallback]
  → prompt.assemble() [dedup, char cap, [S1] tags, boundary tags]
  → llm.chat() [Cloudflare or MOCK]
  → citations built from used chunks
  → _query_response() [includes _trace if PIPELINE_TRACE]
```

**Agentic Chat Path (multi-turn with tools):**
```
POST /chat → api.py:chat → EnterpriseAgent.chat()
  → InputGuardrail.check()
  → session_store.rewrite_query() [memory]
  → detect_intent() [rule-based + optional LLM fallback]
  → slots_for_intent()
  → _decide() [plan: retrieve/memory/tool + requires_approval]
  → AgenticRetriever.iterative_retrieve() [loop up to AGENT_MAX_ITER]
    → retrieve → rerank → assemble → evidence_check() [dense + topical]
    → if not enough: rewrite query → retry
  → Evidence gate: hard refusal if !report.enough (no LLM call)
  → If tool in {create_it_ticket, create_leave_request}:
      → _propose_action() → session_store.set_pending() + workflow.record_proposal() + notifier.notify_new_request()
      → status=needs_approval (C1: side-effect NOT executed)
  → Else: _generate_grounded() [LLM → CitationChecker + GroundingChecker → 1 retry → fallback]
  → OutputGuardrail.check()
  → AgentResponseModel (typed contract)
```

**Approval Execution (C1):**
```
POST /actions/confirm → agent.confirm_action()
  → session_store.pop_pending() [exactly once]
  → execute tool (HRIS or mock)
  → verify result
  → workflow.decide_by_session() + notifier.notify_request_decided()
  → status=action_completed|action_cancelled
```

**Streaming Ingestion Path:**
```
POST /ingest/stream → ChunkProducer.ingest_dir() → transport.produce(topic.doc.chunks)
  → run_workers(n) [thread per worker]
    → EmbeddingWorker.step() → poll → process_message()
      → validate → idempotency check (store.exists) → embed → upsert_one → commit offset
      → on failure: retry with backoff → DLQ + failed event
  → Metrics: lag, throughput, p95 latency (Prometheus /metrics)
```

### Dependency Direction (Current)

```
api.py (FastAPI layer)
  ├─► pipeline_query.py (orchestrator)
  │     ├─► embeddings.py, vector_store.py, retriever.py, reranker.py, prompt.py, llm.py
  │     └─► loops.guardrails, loops.pii, observability.PipelineTracer
  ├─► agent/agent.py
  │     ├─► agent/agentic.py, agent/intents.py, agent/tools.py, agent/session.py, agent/memory.py
  │     ├─► loops.answer_loop (GroundingChecker, CitationChecker)
  │     ├─► loops.guardrails (InputGuardrail, OutputGuardrail)
  │     └─► workflow.py, notifier.py
  ├─► agent/team/orchestrator.py → agent/team/agents.py → agent/team/base.py
  ├─► stream/producer.py, stream/worker.py → stream/transport.py, stream/store.py
  ├─► auth.py, models.py (SQLAlchemy + SQLite)
  ├─► mcp/client.py, mcp/servers.py
  ├─► voice/handler.py
  └─► finetune/export.py, finetune/train.py

config.py (Settings) → imported by ALL modules (central config singleton)
```

---

## 2. Architectural Issues Found

### 2.1 Responsibility Boundary Violations

| # | Issue | File/Module | Why Architectural (not just bug) | Priority |
|---|-------|-------------|----------------------------------|----------|
| RB-01 | **`pipeline_query.py` is a God Module** | `pipeline_query.py` (269 lines) | Contains `build_stack()`, ingestion tail (`_ingest_rawdocs`), URL ingest, source listing, source deletion, **and** the entire query pipeline. Mixes indexing concerns (sanitization, PII scan, tenant enrichment, embedding, upsert) with query concerns (retrieval, rerank, generation, evidence gate). Violates Single Responsibility — changes to ingestion risk breaking query and vice versa. | **Critical** |
| RB-02 | **`EnterpriseAgent` mixes orchestration + domain logic + side-effect coordination** | `agent/agent.py` (640 lines) | Single class handles: input guardrails, memory rewrite, intent detection, slot filling, decision planning, iterative retrieval orchestration, evidence gating, tool proposal, grounded generation, citation checking, output guardrails, session persistence, workflow recording, notification, **and** confirmation execution. Should be split into: `AgentOrchestrator`, `IntentPlanner`, `EvidenceGate`, `ActionProposer`, `ActionExecutor`, `GroundingValidator`. | **Critical** |
| RB-03 | **`api.py` contains business logic (admin endpoints)** | `api.py` lines 559-623, 627-692 | Admin endpoints (`/admin/documents`, `/admin/stats`, `/admin/requests`, `/admin/outbox`, `/admin/activity`) directly call `build_stack()`, `workflow.py`, `notifier.py`, and manipulate Qdrant. This is application service logic leaking into the HTTP adapter layer. Should be in a separate `admin_service.py`. | **Should-fix** |
| RB-04 | **`retriever.py` rebuilds BM25 index on every `rebuild()` call** | `retriever.py:53-75` | `rebuild()` scrolls entire Qdrant collection, tokenizes all texts, builds BM25Okapi, pickles to disk. This is an **indexing-time operation** living in a **query-time component**. Called from `pipeline_query._ingest_rawdocs` (line 104) — circular dependency: query component called from ingestion. | **Critical** |
| RB-05 | **`vector_store.py` contains both Qdrant adapter AND circuit breaker logic** | `vector_store.py:16-32, 139-143` | `_get_qdrant_breaker()`, `_qdrant_retry`, and `with_retry` calls are infrastructure concerns (resilience) mixed with data access. Circuit breaker should be a decorator or middleware, not inline in `search()`. | **Should-fix** |

---

### 2.2 Dependency Inversion / Circular Dependencies

| # | Issue | File/Module | Why Architectural | Priority |
|---|-------|-------------|-------------------|----------|
| DI-01 | **Circular import: `llm.py` ↔ `loops/resilience.py` ↔ `loops/corrective_rag.py` ↔ `llm.py`** | `llm.py:11-12, 48-49`, `loops/corrective_rag.py:12`, `loops/__init__.py:21-28` | `llm.py` lazily imports `CircuitBreaker` from `loops.resilience` inside `_get_llm_breaker()`. But `loops/__init__.py` imports `CorrectiveRetriever` from `corrective_rag`, which imports `llm` (for `RetrievalGrader.llm`). This creates a runtime circular import risk. Currently avoided by lazy import in `llm.py`, but fragile. | **Critical** |
| DI-02 | **`pipeline_query.py` imports `loops.guardrails` and `loops.pii` at module level** | `pipeline_query.py:40, 58` | Ingestion path (indexing) depends on guardrails/pii loops. But `loops/guardrails.py` imports `loops.pii` (line 110), and `loops/__init__.py` exports all loops. If any loop later imports `pipeline_query` (e.g., for `build_stack`), cycle forms. | **Should-fix** |
| DI-03 | **`agent/agent.py` imports `pipeline_query.build_stack` at runtime** | `agent/agent.py:93` | Agent (higher-level orchestrator) depends on lower-level pipeline_query for stack creation. Inverts the natural dependency: pipeline should be a reusable library, agent should compose it via DI, not import its factory function. | **Should-fix** |
| DI-04 | **`config.py` (Settings singleton) imported by 40+ modules** | All modules | Global settings object creates implicit coupling. Modules cannot be tested with different configs without monkeypatching `settings`. Should use dependency injection (pass `Settings` instance) or a config protocol. | **Nice-to-have** |

---

### 2.3 Service/Agent/Queue Integration Issues

| # | Issue | File/Module | Why Architectural | Priority |
|---|-------|-------------|-------------------|----------|
| SI-01 | **No idempotency key on `confirm_action` — double-execution risk** | `agent/agent.py:514-631`, `api.py:822-829` | `confirm_action` uses `session_store.pop_pending()` which removes the pending action. However: (1) If two concurrent requests hit `/actions/confirm` for same session, second gets "no pending" error (safe). But (2) If the *first* request crashes AFTER tool execution but BEFORE `workflow.decide_by_session()` commits, the action is executed but not recorded — retry would re-execute. No idempotency key (e.g., `request_id`) passed from client to guarantee exactly-once. | **Critical** |
| SI-02 | **Streaming workers: no per-message idempotency verification on upsert** | `stream/worker.py:67-75` | Worker checks `store.exists(chunk_id)` before embed, but this is a **separate RPC** from the upsert. Between `exists()` returning False and `upsert_one()` completing, another worker could upsert the same chunk (race). The stable UUID `uuid5(chunk_id)` makes upsert idempotent, but the *embedding computation* is wasted. Worse: if `exists()` fails (network), it falls through to embed+upsert (line 74-75) — safe but duplicates embedding work. | **Should-fix** |
| SI-03 | **DLQ handling: no replay mechanism, no dead-letter consumer** | `stream/worker.py:106-113`, `stream/events.py` | Failed chunks go to `topic.doc.chunks.dlq` AND `topic.doc.embedding.failed`. But there is **no consumer** for DLQ, no admin API to inspect/replay, no alerting on DLQ growth. `reliability_loop.alert()` only watches lag and embedding_failures_total. DLQ can grow silently. | **Should-fix** |
| SI-04 | **Team orchestrator: no idempotency on member delegation** | `agent/team/orchestrator.py:76-117` | Router delegates to member agents via `TeamBus.post()`. If a member agent crashes mid-handling, the orchestrator has no retry/timeout for that specific hop — it only has a global soft timeout. No idempotency key on the task message. | **Nice-to-have** |
| SI-05 | **LLM-as-judge in CRAG: `RetrievalGrader` trusts LLM output without verification** | `loops/corrective_rag.py:47-89` | `RetrievalGrader.grade()` calls `llm.chat()` and parses JSON from response. If LLM returns malformed JSON or hallucinated grades, the CRAG loop proceeds with garbage grades. No schema validation, no fallback to heuristic grader when LLM fails (though `CRAG_GRADE_USE_LLM=false` defaults to heuristic). | **Should-fix** |

---

### 2.4 Error Handling & Failure Isolation

| # | Issue | File/Module | Why Architectural | Priority |
|---|-------|-------------|-------------------|----------|
| EH-01 | **Silent exception swallowing in `api.py` admin endpoints** | `api.py:664-669`, `api.py:684-691` | `notifier.notify_request_decided()` and `notifier.notify_new_request()` wrapped in bare `except Exception: pass`. If notification fails (SMTP down, network), the admin decision is recorded but the requester is never notified — **no visibility into the failure**. Same in `agent.agent._propose_action()` lines 499-501. | **Critical** |
| EH-02 | **`QdrantStore.search()` returns `[]` on circuit open — masks degradation as "no results"** | `vector_store.py:140-143` | When circuit breaker is OPEN, `search()` catches `CircuitOpenError` and returns empty list. The query pipeline then hits "no candidates" → evidence gate refuses with "no evidence". User sees "no relevant evidence" instead of "system degraded". No distinction between "empty KB" and "Qdrant unavailable". | **Critical** |
| EH-03 | **`EnterpriseAgent.chat()` catches ALL exceptions and returns generic error** | `agent/agent.py:301-311` | `except Exception as e` wraps any error (including programming bugs) into `status=error` with message "Đã xảy ra lỗi...". The actual exception type and traceback are lost (only `type(e).__name__` in flags). Makes debugging production issues extremely hard. | **Should-fix** |
| EH-04 | **`EmbeddingWorker.process_message()` returns `True` (commit offset) even on validation failure** | `stream/worker.py:61-65` | Invalid message (empty text) → `_to_dlq()` → `processed += 1` → `return True` → offset committed. This is correct for *poison pills* (never retryable), but the metric `maia_embeddings_total` is NOT incremented, while `maia_dlq_total` IS. Monitoring sees DLQ growth but no corresponding processed count — misleading. | **Should-fix** |
| EH-05 | **`HybridRetriever.retrieve()` has silent fallback on tenant filter TypeError** | `retriever.py:105-108` | `store.search()` called with `tenant_id`; if `TypeError` (old store signature), retries without tenant_id. This silently **drops tenant isolation** for that query — cross-tenant data leak risk. Should fail loudly or require store interface compliance. | **Critical** |
| EH-06 | **`OutputGuardrail.check()` mutates the answer string in-place** | `loops/guardrails.py:111-114` | Returns `(is_valid, issues, answer)` where `answer` may be redacted. Caller must use the returned answer, but `EnterpriseAgent._generate_grounded()` (line 286-284) calls `self._output_guard.check(answer)` and only appends issues to answer if invalid — **does not use the returned redacted answer**. PII redaction in output guardrail is effectively no-op. | **Critical** |

---

### 2.5 Config & Security Drift

| # | Issue | File/Module | Why Architectural | Priority |
|---|-------|-------------|-------------------|----------|
| CS-01 | **Hardcoded Cloudflare credentials in `.env` (committed)** | `.env:23-24` | `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` are real credentials committed to repo. `.gitignore` does not ignore `.env`. This is a **secret leak**. | **Critical** |
| CS-02 | **JWT_SECRET_KEY has a dev default in code, not just .env** | `config.py:87-88`, `auth.py:21-35`, `api.py:41-50` | `JWT_SECRET_KEY` default is `""` in Settings, but both `auth.py` and `api.py` generate ephemeral random key if empty. This means: (1) tokens invalid on every restart, (2) no warning in `config.py` itself, (3) two different code paths generate the key (divergence risk). | **Should-fix** |
| CS-03 | **CORS_ORIGINS defaults to `["*"]` when empty** | `api.py:96` | `allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()] or ["*"]`. Empty config → wildcard. In production, this allows any origin to call the API with credentials. Should default to `[]` (same-origin) and require explicit config. | **Critical** |
| CS-04 | **`ROLE_EMAIL_ALLOWLIST` and related config hardcoded in `config.py`** | `config.py:115-126` | 10+ email addresses and prefixes hardcoded in Settings class. These are **policy data**, not configuration. Should be externalized to a JSON/YAML file or database, loaded at startup. Changing them requires code change + redeploy. | **Should-fix** |
| CS-05 | **SQLite auth DB path hardcoded in `api.py`** | `api.py:60` | `SQLALCHEMY_DATABASE_URL = "sqlite:///./maia_auth.db"` — not configurable via Settings. In production (Render), this file is ephemeral; users lost on restart. Should use `settings.AUTH_DB_PATH` or similar. | **Should-fix** |
| CS-06 | **`BOOTSTRAP_FIRST_ADMIN=true` by default** | `config.py:106`, `api.py:273-274` | First registered user becomes admin automatically. Convenient for demo, dangerous for production (race condition: first attacker to register gets admin). Should default `false`. | **Should-fix** |

---

### 2.6 Consistency & Code Hygiene

| # | Issue | File/Module | Why Architectural | Priority |
|---|-------|-------------|-------------------|----------|
| CC-01 | **Duplicate `UrlIngestReq` definition in `api.py`** | `api.py:199-202` and `api.py:752-755` | Same Pydantic model defined twice (lines 199 and 752). Second one shadows first but misses `tenant_id` field (intentional per comment). Confusing, error-prone. | **Should-fix** |
| CC-02 | **`build_stack()` called in 8+ places with different tenant_id handling** | `pipeline_query.py:16`, `api.py:702, 713, 718, 759, 769, 777, 995, 1005` | Each call site passes `tenant_id` differently (some from auth, some from request, some None). No centralized "get stack for current context" function. Inconsistent tenant resolution. | **Should-fix** |
| CC-03 | **Two different chat history stores: `session_store` (in-memory) vs `chat_sessions.json` (file)** | `agent/session.py`, `storage/chat_sessions.json` | `session_store` is in-memory dict (lost on restart). `chat_sessions.json` appears to be a separate persistence layer (referenced in tests). Unclear which is source of truth. | **Should-fix** |
| CC-04 | **`EnterpriseAgent` and `TeamOrchestrator` both create `EnterpriseAgent` internally** | `agent/team/agents.py:78`, `agent/team/orchestrator.py:29` | `KnowledgeAgent` (in `agents.py`) instantiates `EnterpriseAgent()` in `__init__`. `TeamOrchestrator` also uses `EnterpriseAgent` via members. Creates nested agent instances with separate stacks. Resource waste, inconsistent state. | **Should-fix** |
| CC-05 | **Dead code: `ConfluentKafkaTransport` imported but never used in tests** | `stream/broker_kafka.py`, `stream/transport.py:132-139` | Real Kafka transport exists but CI uses `inmemory` only. No integration test for real Kafka path. `broker_kafka.py` is untested production code. | **Nice-to-have** |
| CC-06 | **Inconsistent error response shapes across endpoints** | `api.py` | Some endpoints return `{"ok": False, "error": "..."}`, others raise `HTTPException`, others return `{"status": "error", ...}`. No unified error envelope. Client must handle multiple formats. | **Nice-to-have** |

---

## 3. Priority Summary

### Critical (Fix Before Production/Deploy)
1. **RB-01** God Module `pipeline_query.py` — split ingestion vs query
2. **RB-02** `EnterpriseAgent` monolith — decompose into focused classes
3. **RB-04** Retriever rebuilds BM25 in query component — move to indexing service
4. **DI-01** Circular import `llm` ↔ `loops.resilience` ↔ `corrective_rag` — break cycle
5. **SI-01** No idempotency key on `confirm_action` — double-execution risk
6. **EH-01** Silent exception swallow in notifications — lost alerts
7. **EH-02** Circuit open returns `[]` → masks degradation as "no evidence"
8. **EH-05** Silent tenant filter drop on TypeError — cross-tenant leak risk
9. **EH-06** OutputGuardrail redaction not used — PII leaks in output
10. **CS-01** Real Cloudflare credentials committed in `.env` — **IMMEDIATE ROTATE**
11. **CS-03** CORS defaults to `*` — wildcard in production

### Should-Fix (Before Demo/Portfolio)
12. **RB-03** Admin business logic in `api.py` — extract to service
13. **RB-05** Circuit breaker inline in vector store — extract to decorator
14. **DI-02** Pipeline imports loops at module level — lazy import or invert
15. **DI-03** Agent imports `build_stack` — use DI
16. **SI-02** Worker idempotency check race — atomic check-and-upsert
17. **SI-03** No DLQ consumer/replay — add admin API + alerting
18. **SI-05** LLM grader trusts output — add JSON schema validation
19. **EH-03** Generic exception catch in agent — log full trace, return structured error
20. **EH-04** Validation failure commits offset but not counted — fix metrics
21. **CS-02** JWT secret generation divergence — centralize in config
22. **CS-04** Role email allowlist hardcoded — externalize to config file
23. **CS-05** Auth DB path hardcoded — make configurable
24. **CS-06** `BOOTSTRAP_FIRST_ADMIN` default true — default false
25. **CC-01** Duplicate `UrlIngestReq` — consolidate
26. **CC-02** `build_stack` called inconsistently — centralize tenant resolution
27. **CC-03** Two chat history stores — unify
28. **CC-04** Nested `EnterpriseAgent` in team agents — share single instance

### Nice-to-Have (Technical Debt)
29. **DI-04** Global Settings singleton — use DI
30. **CC-05** Untested `ConfluentKafkaTransport` — add integration test or remove
31. **CC-06** Inconsistent error envelopes — unify
32. **SI-04** Team orchestrator no per-hop idempotency — add task IDs

---

## 4. Clarifying Question

**Question:** The `EnterpriseAgent` is instantiated in multiple places with different dependency injection patterns:
- `api.py:788` creates `EnterpriseAgent(tenant_id=current_user.tenant_id)` per request
- `agent/team/agents.py:78` creates `EnterpriseAgent()` inside `KnowledgeAgent.__init__`
- `api.py:975` creates `EnterpriseAgent(tenant_id=current_user.tenant_id)` for voice
- `agent/agent.py:79` accepts optional `embedder, store, retriever, reranker, llm` for testing

**What is the intended lifecycle and sharing model for `EnterpriseAgent` and its dependencies (retriever, store, embedder)?** Should there be a single shared retriever/store per tenant (singleton per process), or per-request instances? This affects connection pooling, BM25 cache coherence, and whether `retriever.rebuild()` called from ingestion is visible to in-flight queries.