import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.chunking import split_documents
from maia.ingestion import RawDoc
from maia.prompt import assemble


def test_chunk_metadata():
    docs = [RawDoc(text="Hello world. " * 200, metadata={"doc_id": "d1", "filename": "a.md"})]
    chunks = split_documents(docs, chunk_size=512, chunk_overlap=50)
    assert len(chunks) >= 2
    assert all("chunk_id" in c.metadata for c in chunks)


def test_assemble_caps():
    cands = [{"chunk_id": f"c{i}", "text": "x " * 500, "metadata": {"filename": "f"},
              "fused_score": 1.0} for i in range(10)]
    ctx, used = assemble(cands, max_chars=3000)
    assert len(ctx) <= 4000
    assert len(used) < 10
