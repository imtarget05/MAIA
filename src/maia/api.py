"""FastAPI application layer (§9)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel

from maia.config import settings
from maia.pipeline_query import ingest_data_dir, query, build_stack

app = FastAPI(title="MAIA RAG Knowledge Platform", version="0.1.0")


class QueryReq(BaseModel):
    question: str
    top_k: int | None = None


@app.get("/health")
def health():
    _, store, _, reranker, llm = build_stack()
    return {"status": "ok", "qdrant_points": store.count(),
            "collection": settings.QDRANT_COLLECTION,
            "llm_mode": llm.mode, "rerank_mode": reranker.mode,
            "embed_model": settings.EMBED_MODEL}


@app.post("/ingest")
def ingest():
    return ingest_data_dir()


@app.post("/ingest/upload")
async def ingest_upload(files: list[UploadFile] = File(...)):
    dest = Path(settings.DATA_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        (dest / f.filename).write_bytes(await f.read())
    return ingest_data_dir(str(dest))


@app.post("/query")
def do_query(req: QueryReq):
    return query(req.question, top_k_final=req.top_k)


@app.get("/collections/count")
def count():
    _, store, _, _, _ = build_stack()
    return {"collection": settings.QDRANT_COLLECTION, "points": store.count()}
