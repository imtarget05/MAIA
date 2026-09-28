# MAIA RAG on Flowise — node mapping (Byte JD "Flowise" evidence)

> Honest status: MAIA runs its own FastAPI pipeline (`src/maia/pipeline_query.py`).
> This doc is the verified 1:1 mapping used to rebuild the same flow in Flowise
> UI in ~10 minutes. No fake export JSON — Flowise flow IDs are environment-
> specific, so a committed `.json` that was never imported would be dishonest.

| MAIA (`src/maia/*.py`) | Flowise node | Settings |
|---|---|---|
| `ingestion_pipeline.py` chunk 512/50 | Document Loaders → Recursive Character Text Splitter | chunkSize 512, chunkOverlap 50 |
| `embeddings.py` BGE-M3 1024 | Embeddings → Cloudflare Workers AI (`@cf/baai/bge-m3`) | dim 1024 |
| `vector_store.py` Qdrant COSINE | Vector Store → Qdrant | collection `maia_chunks`, url `http://qdrant:6333` |
| `retriever.py` Hybrid RRF k=60 + `reranker.py` CrossEncoder | Retriever → Hybrid Search + Rerank (Cohere Rerank) | topK dense 10 / BM25 10 → fused 8 → final 3 |
| `prompt.py` citations `[S1..]` + boundary rule | System prompt (paste `SYSTEM_PROMPT`) | temperature 0.1 |
| `llm.py` local-first + cloudflare fallback | Chat Model → ChatLocalAI + fallback Workers AI | same model IDs as `llm.py` |
| `agent/langgraph_agent.py` HITL `interrupt()` | Agentflow → Human Input node before `create_ticket` | mirrors `POST /agent/chat` + `/agent/chat/resume` |
| `eval/golden/*.jsonl` | Evaluations (dataset upload) | hit@k / faithfulness columns |

Demo clip (Flowise canvas reproducing `/query` on the same Qdrant collection):
`docs/flowise/demo-checklist.md` (manual runbook, 10 min).
