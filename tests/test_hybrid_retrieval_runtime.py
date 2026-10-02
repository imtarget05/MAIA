"""Runtime proof that hybrid retrieval is HYBRID, not dense in a trench coat.

WHY THIS IS SEPARATE FROM THE RETRIEVER'S OWN UNIT TESTS. Those run in the
developer's full virtualenv, where every optional dependency happens to be
installed, so they cannot tell a working sparse leg from a silently disabled
one. The failure this guards is precisely silent: `retriever._build_bm25`
swallows the ImportError, `retrieve()` gates the sparse leg on `self._bm25`, and
the answer still looks like a successful RAG response.

So every assertion here is on an OBSERVED RESULT, never on an import or a
source line:

  * the BM25 index is actually built (`retriever._bm25 is not None`);
  * a rare literal identifier is retrievable ONLY by the lexical leg;
  * the dense leg returns results independently;
  * RRF consumed both lists, proven by a chunk that ranks from both;
  * tenant isolation holds on the sparse path, which is the path most likely to
    leak because the corpus cache is a separate object from the vector store.
"""

from __future__ import annotations

import tempfile

import pytest

from maia.retriever import HybridRetriever

# A rare, lexically decisive identifier. Dense retrieval has no reason to rank
# this highly — it shares no embedding neighbourhood with the query. BM25 does.
RARE_IDENTIFIER = "ZQX-7741-BRAVO"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class _Store:
    """In-memory vector store exposing only what HybridRetriever touches."""

    def __init__(self, chunks: list[dict], dense_ranking: dict[str, list[str]]):
        self._chunks = chunks
        self._dense_ranking = dense_ranking
        self.search_calls = 0

    def scroll_all(self, tenant_id=None):
        if tenant_id is None:
            return list(self._chunks)
        return [
            c
            for c in self._chunks
            if c.get("metadata", {}).get("tenant_id") == tenant_id
        ]

    def search(self, vec, top_k=10, tenant_id=None):
        self.search_calls += 1
        pool = [
            c
            for c in self._chunks
            if tenant_id is None
            or c.get("metadata", {}).get("tenant_id") in (tenant_id, None, "")
        ]
        order = self._dense_ranking.get(str(vec[0]), [])
        by_id = {c["chunk_id"]: c for c in pool}
        ranked = [by_id[i] for i in order if i in by_id]
        ranked += [c for c in pool if c["chunk_id"] not in order]
        return [{**c, "score": 1.0 - i * 0.05} for i, c in enumerate(ranked[:top_k])]


class _Embedder:
    """Deterministic embedder; the vector selects which dense ranking to use."""

    def embed_query(self, query: str):
        return [1.0] * 384


def _chunk(chunk_id: str, text: str, tenant: str) -> dict:
    return {"chunk_id": chunk_id, "text": text, "metadata": {"tenant_id": tenant}}


@pytest.fixture
def hybrid():
    """A retriever over a corpus where dense and sparse deliberately disagree.

    Dense prefers `dense-only`. Sparse prefers the chunk holding the rare
    identifier. A third chunk carries both signals, so it must accumulate two
    RRF contributions — that is what makes the fusion observable.
    """
    chunks = [
        _chunk("dense-only", "The remote work policy allows two home days.", TENANT_A),
        _chunk(
            "sparse-only",
            f"Inventory asset tag {RARE_IDENTIFIER} must be returned to stores.",
            TENANT_A,
        ),
        _chunk(
            "both",
            f"Asset {RARE_IDENTIFIER} follows the remote work policy for staff.",
            TENANT_A,
        ),
        _chunk(
            "tenant-b-secret", f"Tenant B private note {RARE_IDENTIFIER}.", TENANT_B
        ),
    ]
    store = _Store(chunks, dense_ranking={"1.0": ["dense-only", "both", "sparse-only"]})
    retriever = HybridRetriever(
        store,
        _Embedder(),
        storage_dir=tempfile.mkdtemp(prefix="hybrid-proof-"),
        tenant_id=TENANT_A,
    )
    return retriever, store


def test_bm25_index_is_actually_built(hybrid) -> None:
    """The sparse index must exist. `None` is the silent-degradation state."""
    retriever, _ = hybrid
    assert retriever._bm25 is not None, (
        "BM25 index was not built — the sparse leg is dead and hybrid retrieval is "
        "dense-only. Check that rank-bm25 is in requirements.api.txt."
    )
    assert retriever._corpus, "corpus empty; nothing for BM25 to index"


def test_sparse_leg_actually_executes(hybrid) -> None:
    """A rare literal must be retrievable, and only the sparse leg can get it."""
    retriever, _ = hybrid
    hits = retriever.retrieve(RARE_IDENTIFIER, tenant_id=TENANT_A)
    by_id = {h["chunk_id"]: h for h in hits}
    assert "sparse-only" in by_id, f"lexical hit missing; got {sorted(by_id)}"
    assert by_id["sparse-only"]["bm25_score"] > 0, (
        "the chunk was found but carries no bm25_score — it came from the dense "
        "leg, so this does not prove the sparse leg ran"
    )


def test_dense_leg_actually_executes(hybrid) -> None:
    """Dense results must appear too, carrying dense_score."""
    retriever, store = hybrid
    hits = retriever.retrieve("remote work policy", tenant_id=TENANT_A)
    assert store.search_calls >= 1, "the vector store was never queried"
    dense_rows = [h for h in hits if h.get("dense_score", 0) > 0]
    assert dense_rows, f"no dense results; got {hits}"


def test_rrf_consumes_both_ranking_lists(hybrid) -> None:
    """RRF must fuse two lists, proven by a chunk that ranks from both.

    This is the assertion that distinguishes HYBRID from DENSE-ONLY. If the
    sparse leg were disabled, `both` would accumulate a single contribution and
    could not outrank the chunk dense ranked first.
    """
    retriever, _ = hybrid
    hits = retriever.retrieve(RARE_IDENTIFIER, tenant_id=TENANT_A)
    by_id = {h["chunk_id"]: h for h in hits}
    assert "both" in by_id, f"chunk carrying both signals missing; got {sorted(by_id)}"
    both = by_id["both"]
    assert both["dense_score"] > 0 and both["bm25_score"] > 0, (
        "expected a chunk ranked by BOTH legs; dense="
        f"{both['dense_score']} sparse={both['bm25_score']}"
    )
    assert both["fused_score"] > 0, "fused_score is zero, so RRF did not combine"
    assert both["fused_score"] > by_id["dense-only"]["fused_score"], (
        f"RRF is not fusing both lists: both={both['fused_score']} "
        f"dense-only={by_id['dense-only']['fused_score']}"
    )


def test_tenant_isolation_holds_on_the_sparse_path(hybrid) -> None:
    """Tenant B's chunk carries the same identifier and must not surface.

    The sparse path is the dangerous one: the BM25 corpus cache is a different
    object from the vector store, so a filter applied in `search()` says nothing
    about what `get_scores()` returns.
    """
    retriever, _ = hybrid
    hits = retriever.retrieve(RARE_IDENTIFIER, tenant_id=TENANT_A)
    leaked = [
        h["chunk_id"]
        for h in hits
        if h.get("metadata", {}).get("tenant_id") == TENANT_B
    ]
    assert not leaked, f"CROSS-TENANT LEAK on the sparse path: {leaked}"
    for h in hits:
        assert h.get("metadata", {}).get("tenant_id") in (TENANT_A, None, ""), (
            f"unexpected tenant in results: {h.get('metadata')}"
        )
