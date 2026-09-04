# MAIA — Intelligent RAG Knowledge Platform

> A production-style RAG (Retrieval-Augmented Generation) knowledge platform: ingests documents, creates embeddings, retrieves relevant context from a vector database, and grounds LLM responses on retrieved project knowledge — with hybrid retrieval, reranking, and formal evaluation.

## Pipeline

```
Documents → Ingestion → Chunking → Embedding → Vector Store (Qdrant)
                                                     ↑ retrieval
User Question → Query Embedding → Hybrid Retrieval (dense + BM25 → RRF)
             → Rerank → Context Assembly → LLM → Grounded Answer + Citations
```

## Architecture

```
src/maia/
├── config.py           # Central settings (pydantic-settings, reads .env)
├── ingestion.py        # Parse & clean .md/.txt/.pdf → RawDoc + metadata
├── chunking.py         # LlamaIndex SentenceSplitter + sliding-window fallback
├── embeddings.py       # FastEmbed BAAI/bge-small-en-v1.5 (384-dim) + hash fallback
├── vector_store.py     # Qdrant adapter: upsert, dedup, search, delete_by_doc
├── retriever.py        # Hybrid: dense (cosine) + BM25 → RRF fusion (k=60)
├── reranker.py         # CrossEncoder ms-marco-MiniLM-L-6-v2 (+ fallback)
├── prompt.py           # Context assembly (cap chars, dedup, [S1] cite markers)
├── llm.py              # Cloudflare Workers AI llama-3.1-8b-instruct (+ MOCK mode)
├── pipeline_query.py   # Orchestrator: build_stack(), ingest_data_dir(), query()
├── api.py              # FastAPI application layer
├── cli.py              # CLI entrypoint
└── eval.py             # Formal RAG evaluation metrics
```

## Stack

- **Framework:** LlamaIndex core (`SimpleDirectoryReader`, `SentenceSplitter`, `Document`)
- **Embeddings:** HuggingFace `BAAI/bge-small-en-v1.5` (384-dim) via **FastEmbed** (ONNX, no torch required)
- **Vector DB:** **Qdrant** — collection `maia_knowledge`, COSINE distance, Docker local
- **Retrieval:** hybrid **dense + BM25** fused with **RRF (k=60)**
- **Rerank:** cross-encoder `ms-marco-MiniLM-L-6-v2` (falls back to RRF order without torch)
- **LLM:** **Cloudflare Workers AI** `@cf/meta/llama-3.1-8b-instruct` — automatic **MOCK mode** when no credentials (works fully offline)
- **API:** FastAPI · **UI:** Streamlit with citation/evidence display

## Quickstart

```bash
cp .env.example .env   # fill CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN (optional — mock works offline)
docker compose up -d qdrant
pip install -r requirements.txt
```

### CLI

```bash
PYTHONPATH=src python -m maia.cli ingest
PYTHONPATH=src python -m maia.cli query "Hybrid search hoạt động thế nào?"
PYTHONPATH=src python -m maia.cli health
```

### Full demo

```bash
./run_demo.sh   # ingest → query → eval
```

### API

```bash
PYTHONPATH=src uvicorn maia.api:app --reload --port 8000
```

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | System status, points count, modes |
| POST | `/ingest` | Ingest `DATA_DIR` (default `data/samples`) |
| POST | `/ingest/upload` | Upload and ingest files (.md/.txt/.pdf) |
| POST | `/query` | `{ "question": "...", "top_k": 3 }` |
| GET | `/collections/count` | Points in collection |

### UI

```bash
streamlit run app_streamlit.py
```

## Evaluation

```bash
PYTHONPATH=src python -m maia.eval eval/dataset.jsonl
```

Metrics: **hit@k**, **recall@k**, **context precision**, **faithfulness proxy**, **answer relevance proxy**.
Dataset format (JSONL): `{"question": str, "gold_chunk_ids": [...], "gold_keywords": [...]}`

## Tests

```bash
python -m pytest tests/ -q
```

## Features

- [x] Document ingestion, chunking, embedding, vector retrieval
- [x] Hybrid retrieval (dense + BM25 + RRF) — `src/maia/retriever.py`
- [x] Reranking (CrossEncoder + fallback) — `src/maia/reranker.py`
- [x] Context assembly with dedup + char cap + `[S1]` citation markers — `src/maia/prompt.py`
- [x] Grounded answering with explicit "no evidence" guard
- [x] Formal RAG evaluation — `src/maia/eval.py` + `eval/dataset.jsonl`
- [x] Citation/evidence display — `citations[]` in API + Streamlit expanders

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant endpoint |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | FastEmbed model |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 512 / 50 | Chunking params |
| `TOP_K_DENSE/BM25/FUSED/FINAL` | 10/10/8/3 | Retrieval depth |
| `SIMILARITY_THRESHOLD` | 0.3 | Evidence guard on dense score |
| `RRF_K` | 60 | Reciprocal Rank Fusion constant |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | — | Workers AI creds (empty → MOCK mode) |

## Deployment (all free tier)

| Component | Platform | Notes |
|---|---|---|
| Vector DB | [Qdrant Cloud](https://cloud.qdrant.io) — free 1GB cluster | Copy cluster URL + API key into `QDRANT_URL` / `QDRANT_API_KEY` |
| API (FastAPI) | [Render](https://render.com) — free web service | Blueprint included (`render.yaml`); sleeps after 15 min idle, wakes on request |
| UI (Streamlit) | [Streamlit Community Cloud](https://share.streamlit.io) — always-on free | Configure via App → Settings → Secrets (see `.streamlit/secrets.toml.example`) |
| LLM | Cloudflare Workers AI — free tier | Existing `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN`; empty → MOCK mode |

### Deploy steps

1. **Qdrant Cloud:** create free cluster → get URL + API key.
2. **Render:** New → Blueprint → select repo → fill `QDRANT_URL`, `QDRANT_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` when prompted → Deploy. Health check: `GET /health`.
3. **Streamlit Cloud:** New app → select repo → main file `app_streamlit.py` → in Secrets paste the keys from `.streamlit/secrets.toml.example` with real values.
4. **First ingestion:** `POST /ingest` on the Render API URL, or run `python -m maia.cli ingest` locally pointed at Qdrant Cloud (`.env` with the same `QDRANT_URL`/`QDRANT_API_KEY`) — data persists in Qdrant Cloud, so any later restart is stateless-safe.

> **Free-tier note:** Render's filesystem is ephemeral — the BM25 cache (`storage/bm25_corpus.pkl`) is rebuilt automatically from Qdrant on boot (`retriever._load_or_rebuild()`), so no data loss. Ingested uploads land in Qdrant Cloud and survive restarts. Both UI and API are stateless; scale-to-zero only costs a cold-start.

## Cleanup policy

Safe to delete (regenerated automatically): `storage/` (BM25 cache), `qdrant_data/`, `__pycache__/`, `.pytest_cache/` — all covered by `.gitignore`.

Never delete: `src/maia/` core modules, `eval/dataset.jsonl`, `data/samples/`, `requirements.txt`.

## Sample documents

`data/samples/` ships with 3 markdown docs (platform overview, vectors & hybrid search, RAG failure modes) so the system is queryable immediately after `ingest` — no external data needed.
