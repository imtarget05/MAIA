"""Tests for Document Lifecycle — versioning, model changes, soft delete."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.embeddings import Embedder
from maia.loops.document_lifecycle import DocumentLifecycleManager, DocumentManifest
from maia.stream.store import InMemoryVectorStore


def _embedder():
    return Embedder()


def _new_mgr(tmp="**/tmp_lc.json"):
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return DocumentLifecycleManager(InMemoryVectorStore(), _embedder(), index_path=path), path


def test_upload_creates_v1_manifest():
    mgr, path = _new_mgr()
    m = mgr.upload("policy", "Employee policy text about vacation days. " * 40, "policy.pdf")
    assert m.document_version == 1
    assert m.status == "active"
    assert m.filename == "policy.pdf"
    assert len(m.chunk_ids) > 0
    assert m.embedding_version == "v1"
    return path


def test_update_document_bumps_version():
    mgr, path = _new_mgr()
    v1 = mgr.upload("policy", "Version one content about health benefits. " * 30)
    assert v1.document_version == 1
    v2 = mgr.update_document("policy", "Version two content about remote work. " * 30)
    assert v2.document_version == 2
    assert v2.status == "active"
    assert v1.status == "deprecated"  # old manifest deprecated
    assert v1.deleted_at is not None
    assert len(v2.chunk_ids) > 0


def test_get_manifest_and_list_documents():
    mgr, path = _new_mgr()
    mgr.upload("doc_a", "Alpha content. " * 30)
    mgr.upload("doc_b", "Beta content. " * 30)
    m = mgr.get_manifest("doc_a")
    assert isinstance(m, DocumentManifest)
    assert m.document_id == "doc_a"
    docs = mgr.list_documents()
    assert len(docs) == 2


def test_delete_document_soft_deletes():
    mgr, path = _new_mgr()
    mgr.upload("doc_x", "Deletable content. " * 30)
    res = mgr.delete_document("doc_x")
    assert res["status"] == "deleted"
    m = mgr.get_manifest("doc_x")
    assert m.status == "deleted"
    assert m.deleted_at is not None
    # Should not appear in active list.
    assert len(mgr.list_documents()) == 0
    assert len(mgr.list_documents(include_deleted=True)) == 1


def test_change_embedding_model_tags_version():
    """Changing model updates config for FUTURE uploads; existing manifests keep
    their version. A new upload after the change uses the new model."""
    mgr, path = _new_mgr()
    mgr.upload("doc_m", "Model version content. " * 30)
    m1 = mgr.get_manifest("doc_m")
    assert m1.embedding_model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # Change model config.
    res = mgr.change_embedding_model("bge-m3", "v2")
    assert res["model"] == "bge-m3" and res["version"] == "v2"
    # Old manifest keeps its version.
    assert m1.embedding_model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # New upload uses the new model.
    m2 = mgr.upload("doc_n", "New doc after model change. " * 30)
    assert m2.embedding_model == "bge-m3" and m2.embedding_version == "v2"


def test_version_tracking_distinguishes_vectors():
    """After model change + update, old and new vectors carry different version tags."""
    mgr, path = _new_mgr()
    mgr.upload("emp", "Employee policy v1. " * 30, "emp.pdf")
    n1 = len(mgr.get_chunks_by_version("emp", 1))
    assert n1 > 0
    mgr.update_document("emp", "Employee policy v2 updated. " * 30, "emp.pdf")
    n2 = len(mgr.get_chunks_by_version("emp", 2))
    assert n2 > 0
    # v1 chunks still retrievable by version.
    assert len(mgr.get_chunks_by_version("emp", 1)) == n1


def test_manifest_tracks_all_fields():
    mgr, path = _new_mgr()
    m = mgr.upload("full", "Full field content for manifest. " * 30, "full.pdf")
    assert m.document_id == "full"
    assert m.document_version == 1
    assert m.filename == "full.pdf"
    assert m.content_hash != ""
    assert m.embedding_model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert m.embedding_version == "v1"
    assert m.chunking_version == "recursive:v2"
    assert m.chunk_size == 512
    assert m.chunk_overlap == 50
    assert m.status == "active"
    assert m.created_at != "" and m.updated_at != ""
    assert m.deleted_at is None