# MAIA Architecture Refactor Plan

Status: DRAFT. Implementation-ready for an implementation-capable agent. No source code has been modified.

Goal: Controlled, evidence-driven architecture refactoring of the MAIA RAG platform codebase at src/maia/. Preserve all existing behavior unless explicitly authorized.

## 1. Context and goal

MAIA is an enterprise RAG knowledge platform (Vietnamese HR/IT helpdesk). The user asked for a controlled architecture refactoring that improves internal architecture without changing existing system behavior. This plan is the output of the planning phase only; no source code has been modified.

## 2. What was inspected (evidence sources)

Full read of: pyproject.toml, requirements.txt, requirements-kafka.txt, .env.example, src/maia/config.py, models.py, pipeline_query.py, agent/agent.py, agent/agentic.py, agent/session.py, agent/hris.py, agent/tools.py, agent/memory.py, agent/schemas.py, agent/intents.py, agent/teams.py, agent/team/orchestrator.py, loops/__init__.py, loops/guardrails.py, loops/answer_loop.py, loops/pii.py, loops/resilience.py, loops/corrective_rag.py (head), retriever.py, vector_store.py, embeddings.py, llm.py, prompt.py, answer_format.py, ingestion.py, chunking.py, reranker.py, source_reader.py, observability.py, workflow.py (head), notifier.py (head), stream/worker.py, stream/producer.py, stream/transport.py, eval.py (head), cli.py (head), api.py (head), tests/conftest.py. Repo tree listing of src/ (70 .py) and tests/ (31 .py). Git log (6 commits, latest 3826c40). README headings scan.

## 3. Verified architecture facts

- Entry points: FastAPI app in src/maia/api.py (1073 lines; module-level engine + tables created at import). Streamlit in app_streamlit.py (48 KB). CLI via python -m maia.cli.
- Two pipelines: INDEXING (producer -> Kafka/inmemory broker -> embedding workers -> Qdrant) and QUERY (query() in pipeline_query.py: build_stack -> retrieve -> rerank -> assemble -> LLM -> clean_answer -> citations). Agentic path in agent/agent.py: EnterpriseAgent.chat() -> decide -> iterative_retrieve -> evidence gate -> generate_grounded -> output validator.
- Layers: config (pydantic-settings), domain (ingestion/chunking/source_reader/textnorm), retrieval (retriever/vector_store/embeddings/reranker), LLM (llm.py CloudflareLLM with mock fallback), prompt (prompt.py), loops (guardrails/answer_loop/pii/resilience/corrective_rag), agent (agent/agentic/session/hris/tools/memory/schemas/intents/teams), stream (worker/producer/transport/events/metrics/store/broker_kafka), infra (observability/workflow/notifier/mcp/voice/finetune), api (FastAPI), auth (models.py + auth.py).
- External deps: Qdrant (vector), FastEmbed (embeddings, optional), rank-bm25 (sparse), Cloudflare Workers AI (LLM, optional), confluent-kafka (optional, separate requirements-kafka.txt), trafilatura/pypdf/llama-index (ingestion, optional), SQLAlchemy+SQLite (auth/workflow/ltm), stdlib smtplib (notifications).
- Config: single Settings class (src/maia/config.py:5-187), env-file .env, ~90 fields. Feature flags default OFF: CRAG_ENABLED, LTM_ENABLED, TEAM_ENABLED, VOICE_ENABLED, MCP_*_ENABLED, HRIS_ENABLED, USE_LLM_GROUNDING, PIPELINE_TRACE.
- Tests: 31 files, fully offline via conftest setting MAIA_EMBED_FORCE_HASH=1. In-memory broker + in-memory vector store used in tests; no Qdrant/Kafka/localhost services required.
- Error handling: circuit breaker + retry (loops/resilience.py) around Qdrant/embed/LLM; degraded fallbacks (empty results, mock LLM, DLQ for streaming). Evidence gate refuses instead of generating (P0).

## 4. Key observations and unknowns

- pipeline_query.py is the hub: it imports ingestion, chunking, guardrails, pii, observability, and lazily builds the full stack. agent/agent.py also imports pipeline_query.build_stack. This is a documented, lazy-init pattern to avoid circular imports, but it concentrates cross-cutting wiring in one module.
- src/maia/api.py and src/maia/eval.py and src/maia/cli.py insert sys.path hacks (sys.path.insert(0, .../src)) despite pyproject setting pythonpath=["src"] for pytest; these are redundant for tests but needed for direct script execution.
- Mixed responsibilities are present but are mostly intentional layering (e.g. hris.py wraps real HRIS API + mock fallback; tools.py is the mock DB). No business-logic-in-infra violations found that are clearly accidental.
- UNKNOWN: exact acceptance criteria for the refactoring (which architectural smells the user considers in scope). NEEDS_VERIFICATION: whether the user wants the full 10-task plan executed or a subset.

## 5. Identified issues

ISSUE-001 [pipeline_query.py] Hub module mixes cross-cutting wiring + ingestion side effects. Severity MEDIUM. Confidence HIGH. Evidence: pipeline_query.py:1-54 imports ingestion/chunking/guardrails/pii/observability; module singletons _doc_sanitizer (L42), _pii_scanner (L63); _ingest_rawdocs L81 orchestrates chunk->sanitize->pii->enrich->embed->upsert. Impact: no seam to test ingestion isolated from retrieval.

ISSUE-002 [config.py] Duplicate threshold constants. Severity LOW. Confidence HIGH. Evidence: config.py:24 SIMILARITY_THRESHOLD=0.3 vs config.py:44 AGENT_EVIDENCE_THRESHOLD=0.3 (identical calibration comments); loops/answer_loop.py:25 GROUNDING_THRESHOLD=0.15 hardcoded third copy. Impact: drift risk.

ISSUE-003 [api.py, eval.py, cli.py] Redundant sys.path hacks. Severity LOW. Confidence HIGH. Evidence: api.py:7, eval.py:14, cli.py:6 insert sys.path(.../src) while pyproject.toml:9 sets pythonpath=[src]. Impact: fragile, masks package importability.

ISSUE-004 [agent/agent.py] Orchestration + tool policy + presentation in one class. Severity MEDIUM. Confidence HIGH. Evidence: agent.py:337-562 chat()/confirm_action() contain intent detection, slots, retrieval, evidence gate, tool proposal, pending storage, workflow record, notify, response assembly. _propose_action L490 does retrieval+generation+workflow+notify+response in one method. Impact: hard to unit-test single concerns.

ISSUE-005 [agent/hris.py + agent/tools.py] Two implementations of same contract, no shared interface. Severity MEDIUM. Confidence HIGH. Evidence: hris.py:115-189 wraps _try_hris then tools.py mocks; tools.py:32-61 same functions without tenant_id param. Tenant check only in hris.py:66-97. Impact: two sources of truth; direct tools.py tests bypass tenant checks.

ISSUE-006 [agent/session.py] In-memory singleton, no persistence. Severity MEDIUM. Confidence HIGH. Evidence: session.py:16-23 module-level _store/_pending; session.py:112 process-global singleton. Pending proposals lost on restart (acknowledged session.py:96-98). Impact: approval workflow not durable.

ISSUE-007 [loops/answer_loop.py] guarded_generate unused by production paths. Severity LOW. Confidence HIGH. Evidence: only referenced in tests/test_loops.py:94,102,108. pipeline_query.query() (pipeline_query.py:181) and agent.agent.py _generate_grounded (agent.py:258) each inline their own generate+retry+fallback. Impact: duplicated logic, divergence risk.

ISSUE-008 [stream/worker.py] Worker mixes consumption/idempotency/retry/DLQ/metrics. Severity LOW. Confidence MEDIUM. Evidence: worker.py:57-101 process_message handles validation, exists-check, retry, upsert, DLQ, metrics. Double-produce to DLQ + failed topic at L111-112 undocumented. Impact: moderate.

ISSUE-009 [config.py] God object (~90 fields, 14 domains). Severity LOW. Confidence HIGH. Evidence: config.py:5-187 groups Qdrant/embeddings/chunking/retrieval/LLM/storage/URL/Kafka/CRAG/LTM/teams/MCP/voice/auth/email/workflow/PII/observability/UI/reliability. Impact: low coupling but hard to reason about per-module settings.

ISSUE-010 [README.md] Prose-only docs, no machine-readable contract. Severity LOW. Confidence HIGH. Evidence: no API/schema spec; contract only in agent/schemas.py:1-17 and pipeline_query.py:1-3 docstrings. Impact: contract drift risk.

## 5. Identified issues

All issues are LOW or MEDIUM severity. No CRITICAL or HIGH issues found. Confidence is HIGH unless noted.

ISSUE-001 [pipeline_query.py] Hub module mixes cross-cutting wiring + ingestion side effects. Severity MEDIUM. Confidence HIGH. Evidence: pipeline_query.py:1-54 imports ingestion/chunking/guardrails/pii/observability; module singletons _doc_sanitizer (L42), _pii_scanner (L63); _ingest_rawdocs L81 orchestrates chunk->sanitize->pii->enrich->embed->upsert. Impact: no seam to test ingestion isolated from retrieval.

ISSUE-002 [config.py] Duplicate threshold constants. Severity LOW. Confidence HIGH. Evidence: config.py:24 SIMILARITY_THRESHOLD=0.3 vs config.py:44 AGENT_EVIDENCE_THRESHOLD=0.3 (identical calibration comments); loops/answer_loop.py:25 GROUNDING_THRESHOLD=0.15 hardcoded third copy. Impact: drift risk.

ISSUE-003 [api.py, eval.py, cli.py] Redundant sys.path hacks. Severity LOW. Confidence HIGH. Evidence: api.py:7, eval.py:14, cli.py:6 insert sys.path(.../src) while pyproject.toml:9 sets pythonpath=[src]. Impact: fragile, masks package importability.

ISSUE-004 [agent/agent.py] Orchestration + tool policy + presentation in one class. Severity MEDIUM. Confidence HIGH. Evidence: agent.py:337-562 chat()/confirm_action() contain intent detection, slots, retrieval, evidence gate, tool proposal, pending storage, workflow record, notify, response assembly. _propose_action L490 does retrieval+generation+workflow+notify+response in one method. Impact: hard to unit-test single concerns.

ISSUE-005 [agent/hris.py + agent/tools.py] Two implementations of same contract, no shared interface. Severity MEDIUM. Confidence HIGH. Evidence: hris.py:115-189 wraps _try_hris then tools.py mocks; tools.py:32-61 same functions without tenant_id param. Tenant check only in hris.py:66-97. Impact: two sources of truth; direct tools.py tests bypass tenant checks.

ISSUE-006 [agent/session.py] In-memory singleton, no persistence. Severity MEDIUM. Confidence HIGH. Evidence: session.py:16-23 module-level _store/_pending; session.py:112 process-global singleton. Pending proposals lost on restart (acknowledged session.py:96-98). Impact: approval workflow not durable.

ISSUE-007 [loops/answer_loop.py] guarded_generate unused by production paths. Severity LOW. Confidence HIGH. Evidence: only referenced in tests/test_loops.py:94,102,108. pipeline_query.query() (pipeline_query.py:181) and agent.agent.py _generate_grounded (agent.py:258) each inline their own generate+retry+fallback. Impact: duplicated logic, divergence risk.

ISSUE-008 [stream/worker.py] Worker mixes consumption/idempotency/retry/DLQ/metrics. Severity LOW. Confidence MEDIUM. Evidence: worker.py:57-101 process_message handles validation, exists-check, retry, upsert, DLQ, metrics. Double-produce to DLQ + failed topic at L111-112 undocumented. Impact: moderate.

ISSUE-009 [config.py] God object (~90 fields, 14 domains). Severity LOW. Confidence HIGH. Evidence: config.py:5-187 groups Qdrant/embeddings/chunking/retrieval/LLM/storage/URL/Kafka/CRAG/LTM/teams/MCP/voice/auth/email/workflow/PII/observability/UI/reliability. Impact: low coupling but hard to reason about per-module settings.

ISSUE-010 [README.md] Prose-only docs, no machine-readable contract. Severity LOW. Confidence HIGH. Evidence: no API/schema spec; contract only in agent/schemas.py:1-17 and pipeline_query.py:1-3 docstrings. Impact: contract drift risk.