# Current Status Snapshot

<!-- generated-by: repo-harness refresh-current-status v1 -->

> **Status**: Complete — Spec-Alignment Hardening (Option 1)
> **Reason**: EN recall root-caused (hash-mode, not missing data); contact_usability CI gate (≥0.8) wired; spec open questions closed, v1 scope locked

> **Status**: Complete — Patch Weaknesses Full (WS1–WS8 all done, 2026-09-10)
> **Reason**: WS2 head-to-head eval trên Qdrant Cloud xác nhận hybrid recall không giảm → `LLAMA_INDEX_DATA_PLANE=True` mặc định; 262 passed offline

## Slice Progress (2026-09-09 — Patch Weaknesses Full)

- [x] **WS1 — committed tests + pin deps** (xong từ gap closure): 3 test files committed (langgraph/llamaindex/agent_chat), `requirements.txt` pinned `langchain-core==1.6.2 / langgraph==1.2.11 / langgraph-checkpoint-sqlite==3.1.1`.
- [x] **WS5 — guardrails merge**: `OutputValidator` hấp thụ vào `OutputGuardrail.check(answer, is_action_response=...)`; `is_action_response=True` chỉ dùng ở action confirm path — không còn dead paths.
- [x] **WS6 — untangle stream/CRAG/LTM**: `reliability_loop.py` import-time `from maia.stream.metrics` → lazy trong `health_report()`; `ltm_learn` rời hot path `chat()` → opt-in `LTM_LEARN_ON_CHAT` (default False, mới trong `config.py`). Verify: `import maia.agent.agent` không kéo `maia.stream` vào `sys.modules`. **Scope decision: retain cả ba (Kafka/CRAG/LTM) opt-in, không enable-by-default.**
- [x] **WS7 — MLOps run manifest**: `src/maia/eval_manifest.py` (run_id, git_sha, mode hash/fastembed, embed_model/dim, thresholds, flags, metrics); CLI `maia.eval --manifest DIR`; CI job `eval-manifest` upload artifact. Tests: `tests/test_eval_manifest.py`.
- [x] **WS3 — reranker/SSE**: decision (a) — `requirements-rerank.txt` (sentence-transformers + torch) + CI job `reranker` riêng verify CrossEncoder mode; SSE resume contract test (`test_agent_chat_stream_sse_resume_after_hitl` — resume là JSON-only by design: SqliteSaver sync-only, streaming chạy MemorySaver ephemeral).
- [x] **WS4 — eval expansion**: `contact_usability.jsonl` 8→15 rows, `paraphrase.jsonl` 8→15 rows; 2-mode reporting qua manifest `mode` field (hash = lower bound, fastembed = baseline thật).
- [x] **WS8 — JD-MAPPING docs**: `docs/JD-MAPPING.md` (JD ↔ file minh chứng + verify commands + demo script 3 phút flows A/B/D).
- [x] **WS2 — LlamaIndex hybrid flag decision (2026-09-10, đóng slice)**: head-to-head eval trên **Qdrant Cloud** (FastEmbed thật, 8 docs / 25 chunks, top-k=3, 10 golden groups) — hit@k / recall@k / context_precision / mrr **đồng nhất 100%** dense-only vs hybrid ở mọi group → **`LLAMA_INDEX_DATA_PLANE` default flipped True** (`config.py:46`); legacy-path test files pin flag OFF qua autouse fixture (data-plane có test riêng). **262 passed** sau flip. Qdrant Cloud credentials trong `.env` (gitignored) + payload indexes `tenant_id/doc_id/chunk_id` (keyword) đã tạo trên collection `maia_knowledge`.
- **Regression**: **262 passed** offline (`MAIA_EMBED_FORCE_HASH=1 pytest tests/ -q`, Qdrant-dependent suites excluded — environmental).

## Latest Completed Task
## Latest Completed Task

- **LangChain + LangGraph + LlamaIndex three-layer AI stack** (2026-09-09): integrated the full AI framework stack into MAIA as the *control plane* (LangGraph), *abstraction layer* (LangChain), and *data plane* (LlamaIndex — already present, now leveraged). All wired behind the existing retriever/LLM stack with zero behavior change to legacy paths:
  - `src/maia/langchain/llm.py` — `CloudflareLangChainAdapter(BaseChatModel)` wraps `CloudflareLLM` (keeps circuit-breaker/retry/mock fallback); `cloudflare_lcel()` + `bind_maia_tools()` helpers
  - `src/maia/langchain/tools.py` — `TOOL_REGISTRY_LC` (4 `StructuredTool` wrappers over existing `TOOL_REGISTRY`, reusing tenant-auth + mock HR DB)
  - `src/maia/langchain/prompts.py` — `rag_prompt`, `agent_prompt`, `structured_classify_prompt` (`ChatPromptTemplate` wrappers of existing prompts, braces-escaped)
  - `src/maia/agent/langgraph_agent.py` — `AgentState` (Pydantic) + `build_graph()` → compiled `StateGraph` with nodes `classify→[simple_answer|retrieve→rerank→generate→verify_grounding→(propose_action|finalize|retry_retrieve)]`; conditional routing + retry loop; `MemorySaver` checkpointing
  - `requirements.txt` — added `langchain-core>=0.3`, `langgraph>=1.0`, `langgraph-checkpoint-sqlite>=2.0`
  - **Verified end-to-end on real Qdrant data**: knowledge query → `intent=hr_policy`, full RAG path, `has_evidence=True`, `grounding_score=0.77`, real Cloudflare LLM, cited answer; action query → `intent=leave_request`, cited policy answer
  - **Regression**: 69 existing tests pass (agent, loops, grounding, agentic, corrective_rag, approval, threshold_regression)

## Latest Completed Task (this session)

- **LangGraph HITL + SSE + `/agent/chat` + LlamaIndex data plane** (2026-09-09): wired the compiled LangGraph control plane into the FastAPI/Streamlit app layer with verified production paths:
  - `src/maia/agent/langgraph_agent.py` — `node_propose_action` now calls `interrupt(proposal)` to PAUSE the graph for human approval (HITL); on `Command(resume={"approved": bool})` the node re-runs and executes or cancels the tool. Added `get_durable_graph()` (SqliteSaver, lazy, shared across requests) + `resume_from_approval()`. Fixed `node_classify_query` to extract+pass slots (bug: was passing `{}`). Added `_execute_tool` + `_is_approved` helpers. `route_after_classify` keeps `general → retrieve` (conservative: `general` is the knowledge catch-all, not chit-chat).
  - `src/maia/api.py` — NEW `POST /agent/chat` (`AgentChatReq`) backed by the durable graph; non-streaming JSON, SSE streaming (`astream_events`→v3 sync via in-memory graph), and HITL resume via `{"resume": {"approved": bool}}`. `_state_to_response()` maps `AgentState` → legacy chat() shape. SqliteSaver checkpoint file at `storage/agent_checkpoints.db`.
  - `src/maia/llamaindex_store.py` (NEW) — `MaiaQdrantStore(BasePydanticVectorStore)` wraps the existing `QdrantStore` so a LlamaIndex `VectorStoreIndex` reads from the SAME Qdrant collection (no new package needed — `llama_index.vector_stores` is not installed). Wired into the graph's `retrieve` node via opt-in `settings.LLAMA_INDEX_DATA_PLANE` (default False). Includes `_MaiaEmbed` bridge (MAIA Embedder → LlamaIndex `BaseEmbedding`).
  - `app_streamlit.py` — NEW "🤖 LangGraph Agent (thử nghệm)" sidebar toggle + `_ask_langgraph()` + `_langgraph_resume()` that call `/agent/chat` API; approval buttons branch on the toggle.
  - `src/maia/config.py` — added `LLAMA_INDEX_DATA_PLANE: bool = False`.
  - `src/maia/agent/session.py` — `_connect` now uses `check_same_thread=False` so the LangGraph worker-thread graph invoke can share the SQLite session store.
  - **Verified**: knowledge query → `answered`; leave_request full-slots → `needs_approval` (proposal in interrupt value); approved resume → `action_completed` (balance 12→10); rejected → `action_cancelled`. All 4 `/agent/chat` modes pass via FastAPI TestClient. LlamaIndex wrapper + `VectorStoreIndex.from_vector_store` + `as_retriever` all verified against a fake store.
  - **Regression**: 219 passed, 2 skipped (8 Qdrant-ConnectionError tests excluded — environmental, pre-existing).

## Latest Completed Task (gap closure — 2026-09-09)

- **Closed all 4 documented gaps** on the LangGraph HITL + SSE + `/agent/chat` + LlamaIndex work:
  1. **Streaming hardened** — `src/maia/api.py::_agent_chat_stream` uses stable `graph.stream(stream_mode="updates")` (LangGraph production API), NOT experimental `stream_events` v3. Maps `(node_name, partial_state)` → `event: node` frames + `event: done` final. Sync-only by design.
  2. **LlamaIndex hybrid done** — `_llama_index_retrieve()` in `langgraph_agent.py` fuses LlamaIndex dense (`VectorStoreIndex` over `MaiaQdrantStore`, same Qdrant collection) + BM25 from existing `HybridRetriever` via RRF k=60. Opt-in `settings.LLAMA_INDEX_DATA_PLANE` (default False); default path unchanged (`HybridRetriever`).
  3. **Temp tests converted** — all `_tmp_*.py` scripts converted to committed tests: `tests/test_langgraph_agent.py` (18 tests: nodes, routing, HITL interrupt/resume via real graph invoke), `tests/test_llama_index_dataplane.py` (12 tests: add/query/delete/tenant-isolation/Index-integration + hybrid RRF), `tests/test_agent_chat_api.py` (11 tests: answered/needs_approval/approve→12→10/reject→cancelled/SSE). No `_tmp_*.py` left in repo root.
  4. **SqliteSaver sync-only documented** — `get_durable_graph()` docstring notes `langgraph-checkpoint-sqlite` has no async streaming support; API intentionally splits: streaming on in-memory `MemorySaver` graph (ephemeral, non-resumable), HITL on durable SqliteSaver graph (`storage/agent_checkpoints.db`).
- **Fixed along the way**: `test_llama_index_dataplane.py` syntax error (stray `])` + duplicated second-half module); `FakeQdrantStore.search` hit shape (`chunk_id/text/score/metadata`, matching real `QdrantStore`); `_FakeBackend.search` tenant filter (`in (tenant_id, None, "")`); `langgraph_agent.py` relative imports (`...x` → `maia.x`); `test_langgraph_agent.py` `_stack_tuple` removal + interrupt-via-graph-invoke rewrite.
- **Regression**: **260 passed, 2 skipped** (Qdrant-dependent golden/tracing excluded — environmental `ConnectionError`, pre-existing). Versions: Python 3.14.7, langchain-core 1.6.2, langgraph 1.2.11, langgraph-checkpoint-sqlite 3.1.1, llama-index-core 0.14.24.

## Next

- Deferred ledger (`tasks/todos.md`): guardrails merge, stream/CRAG/LTM re-evaluation, retrieval-depth improvement (cross-encoder rerank / top-k tuning)
- Prior Simplification work (Steps 1-4) unchanged — see plan `plans/2026-09-09-spec-alignment-hardening.md`
