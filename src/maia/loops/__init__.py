"""PRODUCTION RAG loops for MAIA — 2 pipelines + 5 production loops.

MAIA is designed as two pipelines plus five operational loops so the system is a
*production* RAG, not just "PDF -> embed -> Qdrant":

  INDEXING PIPELINE  : parse -> chunk -> (Kafka -> embedding workers) -> Qdrant
  QUERY PIPELINE     : rewrite -> hybrid retrieval -> rerank -> context -> generate
                         -> grounding check -> answer

  Loop 1  Knowledge loop        (refresh / delete / re-index, change detection)  -> knowledge_loop.py
  Loop 2  Retrieval-quality loop (Recall@K, Precision@K, MRR, NDCG, citations)   -> retrieval_loop.py
  Loop 3  Answer-quality loop    (grounding + citation checker, retry, fallback) -> answer_loop.py
  Loop 4  Evaluation loop        (golden dataset, baseline BM25 vs hybrid)        -> evaluation_loop.py
  Loop 5  Reliability loop       (retry/backoff/DLQ, alert, lag, p95)             -> reliability_loop.py

  RAG Guardrails — input/output safety + prompt injection defense (boundary tags)  -> guardrails.py
  Document Lifecycle — full versioning (doc_id, version, embedding/chunking ver)  -> document_lifecycle.py
  Corrective RAG (CRAG)        — retrieval grading, refinement, web fallback      -> corrective_rag.py
"""

from .corrective_rag import (
    CorrectiveResult,
    CorrectiveRetriever,
    GradeLabel,
    GradeResult,
    KnowledgeRefiner,
    RetrievalGrader,
    WebSearchFallback,
)