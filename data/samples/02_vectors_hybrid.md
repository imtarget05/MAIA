# Vector Databases and Embeddings

## Embeddings

Embeddings represent semantic similarity as dense vectors. Similar meanings have high cosine similarity.
Model used: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 with 384 dimensions, served via FastEmbed from HuggingFace Hub.

## Qdrant

Qdrant stores vectors with payload metadata. Collection `maia_knowledge` uses COSINE distance.
Points are upserted with chunk_id as deterministic UUID so re-ingest overwrites duplicates.

## Chunking

Chunk size 512 with overlap 50 preserves sentence meaning. Poor chunk boundaries can separate definitions and tables (failure mode 4).
Overlap helps keep context across boundaries.

## Hybrid Search and Reranking

Hybrid search = dense (Qdrant cosine) + sparse (BM25 Okapi). Fusion uses Reciprocal Rank Fusion: score = sum(1/(60+rank)).
Reranking uses cross-encoder/ms-marco-MiniLM-L-6-v2 to score query-passage pairs and keep top-3.
