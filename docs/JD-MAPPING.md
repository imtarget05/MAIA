# JD Mapping — MAIA Evidence Index

Bảng ánh xạ yêu cầu JD (ML/LLM Engineer) ↔ minh chứng trong repo. Dùng cho phỏng vấn/demo: mỗi dòng trỏ tới file + test chạy được.

| JD requirement | Minh chứng (file) | Test/verify |
|---|---|---|
| RAG pipeline end-to-end (ingest → chunk → embed → hybrid retrieve → rerank → generate → verify) | `src/maia/pipeline_query.py`, `src/maia/retriever.py`, `src/maia/reranker.py` | `pytest tests/test_security_audit.py` |
| Agent orchestration (LangGraph control plane, stateful routing) | `src/maia/agent/langgraph_agent.py` — classify→retrieve→rerank→generate→verify→propose/finalize | `pytest tests/test_langgraph_agent.py` |
| Framework abstraction (LangChain adapter, LlamaIndex data plane) | `src/maia/langchain/llm.py` (`CloudflareLangChainAdapter`), `src/maia/llamaindex_store.py` | `pytest tests/test_llama_index_dataplane.py` |
| Hybrid retrieval (dense + BM25, RRF k=60) | `src/maia/retriever.py::HybridRetriever`, `langgraph_agent.py::_llama_index_retrieve` | `tests/test_llama_index_dataplane.py::test_hybrid_retrieval_with_bm25_and_rrf` |
| HITL approval (LangGraph interrupt + durable checkpoint) | `langgraph_agent.py::node_propose_action`, `get_durable_graph` (SqliteSaver) | `tests/test_agent_chat_api.py::test_agent_chat_resume_approved` |
| SSE streaming (stable `stream_mode="updates"`) | `src/maia/api.py::_agent_chat_stream` | `tests/test_agent_chat_api.py::test_agent_chat_stream_sse` |
| Security: tenant isolation, prompt-injection defense, PII redaction | `src/maia/loops/guardrails.py`, `src/maia/vector_store.py` (tenant filter) | `pytest tests/test_security_audit.py tests/test_guardrails.py` |
| Guardrails (input sanitize + output secret/PII + approval-claim) | `src/maia/loops/guardrails.py` (merged `OutputGuardrail`) | `tests/test_output_validation.py` |
| Evaluation harness (recall@k, MRR, faithfulness, contact-usability gate) | `src/maia/eval.py`, `eval/golden/*.jsonl` (81+ rows) | `pytest tests/test_golden_eval.py` (cần Qdrant) |
| MLOps: run manifest, Prometheus metrics, Kafka streaming | `src/maia/eval_manifest.py`, `src/maia/stream/metrics.py`, `src/maia/stream/worker.py` | `pytest tests/test_eval_manifest.py tests/test_stream.py`; CI job `eval-manifest` upload artifact |
| Model efficiency (cross-encoder rerank opt-in, embedding fallback) | `src/maia/reranker.py`, `requirements-rerank.txt` | CI job `reranker` |
| Iterative retrieval / CRAG (opt-in) | `src/maia/agent/agentic.py`, `src/maia/loops/corrective_rag.py` | `pytest tests/test_corrective_rag.py` |
| LLM integration (Cloudflare Workers AI, adapter pattern) | `src/maia/llm.py`, `src/maia/agent/langchain_adapter.py` | `tests/test_agent_chat_api.py` (mock LLM offline) |

## AI Engineer Intern — Game Publishing / Marketing & Product Tech (2026-09-28)

| JD requirement | Evidence (file) | Test/verify |
|---|---|---|
| Prompt library under version control (semver, review, rollback) | `prompts/**/<name>.v<semver>.yaml`, `src/maia/promptops/{models,loader,registry}.py` (`PromptRegistry.diff/resolve`) | `pytest tests/test_prompt_library.py -q` |
| Structured output (JSON schema) + contract failures | `src/maia/json_schema_lite.py`, `promptops/evals.py::extract_json` | `pytest tests/test_prompt_evals.py -q` |
| Prompt regression gate (offline, model-free) | `prompts/**/eval_cases` + `run_prompt_evals(golden_only=True)`, `report.regression_gate()` | `tests/test_prompt_evals.py::test_real_library_golden_suite_passes` |
| Agent tool-calling + MCP (Model Context Protocol) | `src/maia/mcp/{protocol,server,client,transport,bridge}.py` — JSON-RPC 2.0, handshake, `tools/list` (cursor), `tools/call` (`isError`) | `pytest tests/test_mcp_protocol.py tests/test_mcp_client.py -q` (includes a real `python -m maia.mcp.bridge` subprocess) |
| Workflow automation over Airtable / Email / MS Teams / Zalo OA | `src/maia/mcp/servers/{airtable_server,notification_server}.py` (idempotent writes, `dry_run` honesty) | `pytest tests/test_mcp_servers.py -q` |
| Data ingestion from product/market sources (API/connector/scraping) | `src/maia/pipeline/collectors.py` (file/JSONL/CSV + opt-in HTTP with cursor pagination), run ledger `ingest_runs` | `pytest tests/test_pipeline.py -q` |
| SQL/DWH insight engine (read-only, guard-enforced) | `src/maia/pipeline/warehouse.py`, `src/maia/sql_guard.py`, `mcp/servers/sql_analytics_server.py` (read-only SQLite + table allowlist) | `tests/test_pipeline.py::test_guard_blocks_dangerous_statements`, `test_sql_analytics_connection_is_read_only` |
| NL→SQL for business questions, refuses instead of guessing | `src/maia/pipeline/nl2sql.py` (6 reviewed rules, diacritic-folded matching, live column verification) | `tests/test_pipeline.py::test_unsupported_question_refuses_instead_of_guessing` |
| LLM fundamentals: token/context budgeting, temperature policy | `docs/PROMPT_ENGINEERING_GUIDE.md` §3, `promptops/evals.py::estimate_tokens`, per-prompt `parameters.rationale` | `tests/test_prompt_evals.py::test_estimate_tokens_is_a_bounded_approximation` |
| LLM evals (retrieval + contract) | `src/maia/eval.py`, `eval/golden/*.jsonl` (semantic) + `promptops/evals.py` (contract) | `pytest tests/test_golden_eval.py tests/test_prompt_evals.py -q` |
| End-to-end demos (review insight, content sync, KPI alert fan-out) | `src/maia/scenarios.py`, `data/market/samples/*`, `POST /api/v1/market/scenarios/run` | `pytest tests/test_scenarios.py -q` |
| Agentic governance (allowlist, per-turn budget, audit) | `src/maia/agent/mcp_dispatch.py`, `MCP_TOOL_ALLOWLIST`, `MCP_MAX_TOOL_CALLS`, `mcp/client.py::AuditSink` (hashed args, no values) | `pytest tests/test_mcp_dispatch.py -q` |

**Demo script (2 phút, offline)**

```bash
curl -s localhost:8000/api/v1/market/scenarios | jq            # 3 scenario
curl -sX POST localhost:8000/api/v1/market/scenarios/run \
  -H 'content-type: application/json' \
  -d '{"name":"review_insight_report","params":{"fixture":"samples/game_reviews.jsonl","game_id":"demo-game"}}' | jq
python -m maia.mcp.bridge --server market_insight --list-tools | jq '.tools[].name'
```


## Demo script 3 phút (flows A/B/D)

1. **Flow A — grounded query** (~60s): `POST /chat` "Chính sách nghỉ phép?" → answer + citations `[S1]`, `grounding_score`. File: `src/maia/pipeline_query.py`.
2. **Flow B — refusal** (~30s): câu hỏi ngoài corpus → `has_evidence=false`, từ chối trả lời. Evidence gate threshold 0.3.
3. **Flow D — action + HITL** (~90s): `POST /agent/chat` "xin nghỉ 5 ngày từ 10/09" → `needs_approval` + `pending_action` card → resume `{"approved": true}` → `action_completed`, balance 12→7. Demo cả SSE: `stream=true` cho `event: node` frames live.

Chạy local: `uvicorn maia.api:app` + Streamlit `app_streamlit.py` (LangGraph Agent mode).
