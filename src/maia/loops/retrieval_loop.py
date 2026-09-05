"""Loop 2 - Retrieval Quality Loop.

Ranking-quality metrics for the QUERY PIPELINE (hybrid retrieval + rerank):

  * recall@k      - fraction of gold chunks recovered in the top-k
  * precision@k   - fraction of top-k that are gold
  * mrr@k         - reciprocal rank of the first relevant (gold) hit
  * ndcg@k        - discounted cumulative gain at k (graded relevance)
  * citation_correctness - of the returned list, how many cited chunks are gold
"""
from __future__ import annotations

from math import log2


def _relevance_ranks(retrieved_ids: list[str], gold: set) -> list[int]:
    """Return an ordered list of relevance labels (0/1) aligned to retrieved order."""
    return [1 if cid in gold else 0 for cid in retrieved_ids]


def recall_at_k(retrieved_ids: list[str], gold: set, k: int | None = None) -> float:
    k = k or len(retrieved_ids)
    top = retrieved_ids[:k]
    if not gold:
        return 0.0
    return len(set(top) & gold) / max(1, len(gold))


def precision_at_k(retrieved_ids: list[str], gold: set, k: int | None = None) -> float:
    k = k or len(retrieved_ids)
    top = retrieved_ids[:k]
    if not top:
        return 0.0
    return len(set(top) & gold) / len(top)


def mrr_at_k(retrieved_ids: list[str], gold: set, k: int | None = None) -> float:
    k = k or len(retrieved_ids)
    for rank, cid in enumerate(retrieved_ids[:k], start=1):
        if cid in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: list[str], gold: set, k: int | None = None) -> float:
    """NDCG@k with graded relevance (rel=1 per gold chunk at its position)."""
    k = k or len(retrieved_ids)
    labels = _relevance_ranks(retrieved_ids[:k], gold)
    # DCG = sum rel_i / log2(rank_i + 1) with rank_i starting at 1
    dcg = sum(lab / max(1.0, log2(rank + 1))
              for rank, lab in enumerate(labels, start=1))
    # Ideal: all relevance grades descend (rel=1 for every relevant, top positions)
    ideal = sorted(labels, reverse=True)
    idcg = sum(rel / max(1.0, log2(rank + 1))
               for rank, rel in enumerate(ideal, start=1))
    return dcg / idcg if idcg else 0.0


def citation_correctness(retrieved_ids: list[str], gold: set) -> float:
    """Fraction of retrieved chunks that are correct/gold (used for citation audit)."""
    if not retrieved_ids:
        return 0.0
    return len(set(retrieved_ids) & gold) / len(retrieved_ids)


def compute_rank_metrics(retrieved_ids: list[str], gold: list[str], k: int = 5) -> dict:
    gold_set = set(gold)
    return {
        "k": k,
        "recall@k": round(recall_at_k(retrieved_ids, gold_set, k), 4),
        "precision@k": round(precision_at_k(retrieved_ids, gold_set, k), 4),
        "mrr@k": round(mrr_at_k(retrieved_ids, gold_set, k), 4),
        "ndcg@k": round(ndcg_at_k(retrieved_ids, gold_set, k), 4),
        "citation_correctness": round(citation_correctness(retrieved_ids, gold_set), 4),
    }