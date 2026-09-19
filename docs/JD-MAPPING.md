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

## Demo script 3 phút (flows A/B/D)

1. **Flow A — grounded query** (~60s): `POST /chat` "Chính sách nghỉ phép?" → answer + citations `[S1]`, `grounding_score`. File: `src/maia/pipeline_query.py`.
2. **Flow B — refusal** (~30s): câu hỏi ngoài corpus → `has_evidence=false`, từ chối trả lời. Evidence gate threshold 0.3.
3. **Flow D — action + HITL** (~90s): `POST /agent/chat` "xin nghỉ 5 ngày từ 10/09" → `needs_approval` + `pending_action` card → resume `{"approved": true}` → `action_completed`, balance 12→7. Demo cả SSE: `stream=true` cho `event: node` frames live.

Chạy local: `uvicorn maia.api:app` + Streamlit `app_streamlit.py` (LangGraph Agent mode).
