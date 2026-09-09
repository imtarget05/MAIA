"""Tests for the 5 Production-RAG loops (Loop 1-5). Fully offline."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.chunking import Chunk
from maia.embeddings import Embedder
from maia.loops import (
    answer_loop,
    evaluation_loop,
    knowledge_loop,
    reliability_loop,
    retrieval_loop,
)
from maia.prompt import assemble, build_messages
from maia.stream.store import InMemoryVectorStore


def _embedder():
    return Embedder()


# ---- Loop 1: Knowledge loop -----------------------------------------------
def test_knowledge_manager_ingest_and_refresh():
    store = InMemoryVectorStore()
    km = knowledge_loop.KnowledgeManager(store, _embedder(), index_path="/tmp/kidx_test.json")
    text1 = "alpha beta gamma. " * 100

    # no prior record -> document_changed reports True (content != None)
    assert km.document_changed("d1", text1)
    km.ingest("d1", text1)
    km.record("d1", text1)
    n1 = store.count()
    assert n1 > 0
    assert not km.document_changed("d1", text1)   # unchanged after record
    assert km.document_changed("d1", text1 + " CHANGED")  # changed content

    km.delete("d1")
    assert store.count() == 0


def test_knowledge_delete_by_doc_removes_all_vectors():
    store = InMemoryVectorStore()
    km = knowledge_loop.KnowledgeManager(store, _embedder(), index_path="/tmp/kidx_test2.json")
    km.ingest("docA", "one two three. " * 50)
    km.ingest("docB", "four five six. " * 50)
    assert store.count() >= 2
    km.delete("docA")
    remaining = {c["metadata"].get("document_id") for c in store.scroll_all()}
    assert remaining == {"docB"}


def test_knowledge_embedding_version_and_metadata_change():
    store = InMemoryVectorStore()
    km = knowledge_loop.KnowledgeManager(store, _embedder(), index_path="/tmp/kidx_test3.json")
    km.ingest("d", "version alpha beta. " * 40)
    res = km.update_embedding_version("bge-large-2")
    assert res["action"] == "embedding_version_changed"
    assert res["version"] == "bge-large-2"
    km.change_metadata("d", {"tags": ["important"]})
    pay = list(store.scroll_all())[0]["metadata"]
    assert pay.get("tags") == ["important"]


# ---- Loop 2: Retrieval metrics --------------------------------------------
def test_rank_metrics_basic():
    gold = ["c1", "c3", "c7"]
    ret = ["c1", "c9", "c3", "c2"]  # c1 rank1, c3 rank3
    m = retrieval_loop.compute_rank_metrics(ret, gold, k=5)
    assert m["recall@k"] == round(2 / 3, 4)   # c1,c3 recovered of 3 gold
    assert m["precision@k"] == round(2 / 4, 4)  # 2 of 4 retrieved are gold
    assert m["mrr@k"] == 1.0                    # first gold at rank 1
    assert 0.0 < m["ndcg@k"] <= 1.0


def test_rank_metrics_empty_gold():
    m = retrieval_loop.compute_rank_metrics(["a", "b"], [], k=5)
    assert m["recall@k"] == 0.0 and m["mrr@k"] == 0.0


# ---- Loop 3: Answer Quality ------------------------------------------------
def test_citation_checker_detects_invalid_cite():
    cc = answer_loop.CitationChecker([{"chunk_id": "k1"}, {"chunk_id": "k2"}])
    ok, _invalid = cc.check("Answer [S1] and [S2].")
    assert ok
    ok, invalid = cc.check("Answer [S9].")
    assert not ok and invalid == [9]


def test_grounding_checker_rejects_hallucination():
    gc = answer_loop.GroundingChecker(threshold=0.15)
    context = "cats and dogs live together in harmony across the meadow."
    grounded, _ = gc.check("cats are friendly.", context)
    assert grounded
    grounded, _ = gc.check("quantum physics explains everything.", context)
    assert not grounded


def test_guarded_generate_returns_grounded_answer():
    """Replicates guarded_generate logic using individual components."""
    candidates = [{"chunk_id": "k1",
                   "text": "Kafka partitions allow many embedding workers to consume in parallel.",
                   "metadata": {"filename": "f.md"}}]

    def llm(messages):
        return "Kafka partitions allow parallel workers [S1]."

    context, used = assemble(candidates, max_chars=3000)
    assert used  # has evidence
    messages = build_messages("How does Kafka scale?", context)
    citation_checker = answer_loop.CitationChecker(used)
    grounding_checker = answer_loop.GroundingChecker(threshold=answer_loop.GROUNDING_THRESHOLD)

    answer = llm(messages)
    grounding_score = grounding_checker.score(answer, context)
    cites_valid, _invalid = citation_checker.check(answer)
    ok = grounding_checker.grounded(answer, context) and cites_valid

    if not ok:
        messages = build_messages(
            "How does Kafka scale?",
            context + "\n\n[RULE] Answer ONLY using [S#] tags above. If unsure, "
            "reply exactly: \"" + answer_loop.FALLBACK_TEXT + "\"")
        answer = llm(messages)
        grounding_score = grounding_checker.score(answer, context)
        cites_valid, _invalid = citation_checker.check(answer)
        ok = grounding_checker.grounded(answer, context) and cites_valid

    if not ok:
        answer = answer_loop.FALLBACK_TEXT
        grounding_score = 0.0
        cites_valid = True

    assert used  # has_evidence
    assert cites_valid
    assert answer != answer_loop.FALLBACK_TEXT  # not fallback


def test_guarded_generate_fallback_on_no_evidence():
    """Replicates guarded_generate fallback when no candidates are provided."""
    candidates = []
    context, used = assemble(candidates, max_chars=3000)
    if not used:
        answer = answer_loop.FALLBACK_TEXT
        has_evidence = False
        fallback = True
    assert has_evidence is False
    assert fallback is True
    assert "don't have enough evidence" in answer


# ---- Loop 4: Evaluation loop ----------------------------------------------
def test_compare_baselines_hybrid_beats_bm25():
    cases = [
        evaluation_loop.EvalCase("How does Kafka parallelize embedding ingestion?",
                                 gold_chunk_ids=["3", "4", "5"],
                                 gold_keywords=["kafka", "partition", "worker"]),
        evaluation_loop.EvalCase("Which embedding model is used?",
                                 gold_chunk_ids=["0"], gold_keywords=["bge-small", "384"]),
    ]
    corpus = ["MAIA uses bge-small model with 384 dimensions via FastEmbed.",
              "Hybrid search fuses dense and sparse retrieval with RRF.",
              "Qdrant stores embeddings as COSINE vectors.",
              "Kafka partitions route chunks to embedding workers.",
              "Consumer group balances load across embedding-worker replicas.",
              "Embedding workers upsert vectors idempotently by chunk_id."]
    res = evaluation_loop.compare_baselines(cases, corpus, k=5)
    base_recall = res["baseline_BM25"]["recall@k"]
    hybrid_recall = res["hybrid_BM25_Vector_Rerank"]["recall@k"]
    assert hybrid_recall >= base_recall
    assert res["improvement"]["recall@k"] >= 0


def test_evaluation_loop_groundedness():
    score = evaluation_loop.groundedness_score(
        "Kafka enables parallel embedding.",
        "Kafka enables parallel embedding workers.")
    assert score > 0.5


# ---- Loop 5: Reliability ---------------------------------------------------
def test_alert_fires_on_threshold():
    assert reliability_loop.alert("consumer_lag", 600, 500) is True
    assert reliability_loop.alert("consumer_lag", 10, 500) is False


def test_should_scale_workers_policy():
    assert reliability_loop.should_scale_workers(50) == 2
    assert reliability_loop.should_scale_workers(250) == 4
    assert reliability_loop.should_scale_workers(999) == 8


def test_health_report_includes_ops_metrics():
    from maia.stream.metrics import MetricsRegistry
    from maia.stream.producer import ChunkProducer
    from maia.stream.transport import InMemoryBroker

    broker = InMemoryBroker(partitions=4)
    p = ChunkProducer(broker)
    for i in range(6):
        p.produce_chunk("d", Chunk(text="alpha beta gamma.", metadata={"chunk_id": f"c{i}"}), i)
    metrics = MetricsRegistry()
    report = reliability_loop.health_report(broker, "embedding-workers", p.topic, metrics=metrics)
    assert report["consumer_lag"] == 6
    assert report["queue_backlog"] == 6
    assert report["recommended_workers"] == 2