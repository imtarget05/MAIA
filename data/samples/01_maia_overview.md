# MAIA Overview

MAIA is a Retrieval-Augmented Generation knowledge platform.
Pipeline: Documents → Processing → Chunking → Embedding → Vector Store → Retrieval → Context → LLM → Grounded Answer.

## RAG Pipeline Steps

1. **Ingestion**: parse PDF, Markdown, TXT files, clean and normalize text.
2. **Chunking**: split into chunks of 512 chars with 50 overlap, attach metadata (doc_id, filename, source, section, page, timestamp, chunk_id).
3. **Embedding**: HuggingFace model BAAI/bge-small-en-v1.5 creates 384-dim vectors.
4. **Vector Store**: Qdrant collection `maia_knowledge` with COSINE distance.
5. **Retrieval**: hybrid search combines dense vector search and BM25 sparse search with RRF fusion, then CrossEncoder reranking.
6. **Generation**: Cloudflare Workers AI model llama-3.1-8b-instruct generates grounded answers with citations [S1], [S2].

## Why RAG instead of fine-tuning

RAG retrieves fresh private documents at query time without retraining. It cites evidence and updates knowledge by re-indexing.
