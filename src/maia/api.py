"""FastAPI application layer (§9)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi import FastAPI, UploadFile, File, Response
from pydantic import BaseModel

from maia.config import settings
from maia.pipeline_query import ingest_data_dir, query, build_stack

app = FastAPI(title="MAIA — Enterprise Employee Assistant", version="0.2.0")


class QueryReq(BaseModel):
    question: str
    top_k: int | None = None
    tenant_id: str | None = None


class ChatReq(BaseModel):
    question: str
    session_id: str | None = None
    employee_id: str | None = None
    top_k: int | None = None
    tenant_id: str | None = None


@app.get("/health")
def health():
    _, store, _, reranker, llm = build_stack()
    return {"status": "ok", "qdrant_points": store.count(),
            "collection": settings.QDRANT_COLLECTION,
            "llm_mode": llm.mode, "rerank_mode": reranker.mode,
            "embed_model": settings.EMBED_MODEL}


@app.post("/ingest")
def ingest(tenant_id: str | None = None):
    return ingest_data_dir(tenant_id=tenant_id)


@app.post("/ingest/enterprise")
def ingest_enterprise(tenant_id: str | None = None):
    """Ingest enterprise docs (data/enterprise) into Qdrant."""
    return ingest_data_dir(settings.ENTERPRISE_DATA_DIR, tenant_id=tenant_id)


@app.post("/ingest/upload")
async def ingest_upload(files: list[UploadFile] = File(...)):
    dest = Path(settings.DATA_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        (dest / f.filename).write_bytes(await f.read())
    return ingest_data_dir(str(dest))


@app.post("/query")
def do_query(req: QueryReq):
    return query(req.question, top_k_final=req.top_k, tenant_id=req.tenant_id)


# ---- Enterprise Agent (Receptionist - Agentic RAG) ----
@app.post("/chat")
def chat(req: ChatReq):
    """Agentic RAG: Decide → Memory(rewrite) → Iterative Retrieve → Evidence → Tool → Grounding."""
    from maia.agent.agent import EnterpriseAgent
    agent = EnterpriseAgent(tenant_id=req.tenant_id)
    return agent.chat(req.question, session_id=req.session_id or "default", employee_id=req.employee_id, top_k_final=req.top_k, tenant_id=req.tenant_id)


@app.post("/chat/stream")
def chat_stream(req: ChatReq):
    """SSE streaming for chat (splits answer into chunks)."""
    from fastapi.responses import StreamingResponse
    from maia.agent.agent import EnterpriseAgent

    agent = EnterpriseAgent(tenant_id=req.tenant_id)

    def gen():
        for chunk in agent.stream_answer(req.question, session_id=req.session_id or "default", employee_id=req.employee_id, tenant_id=req.tenant_id):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/chat/history/{session_id}")
def chat_history(session_id: str):
    from maia.agent.session import session_store
    return {"session_id": session_id, "history": session_store.history(session_id)}


@app.delete("/chat/history/{session_id}")
def chat_history_clear(session_id: str):
    from maia.agent.session import session_store
    session_store.clear(session_id)
    return {"cleared": session_id}


@app.get("/tools/leave/balance")
def leave_balance(employee_id: str = "emp_001"):
    from maia.agent.tools import check_leave_balance, get_employee_requests
    return {"balance": check_leave_balance(employee_id), "requests": get_employee_requests(employee_id)}


@app.post("/tools/leave/request")
def leave_request(employee_id: str = "emp_001", days: int = 1, start_date: str | None = None):
    from maia.agent.tools import create_leave_request
    return create_leave_request(employee_id, days, start_date)


@app.post("/tools/it/ticket")
def it_ticket(employee_id: str = "emp_001", ticket_type: str = "general", description: str = ""):
    from maia.agent.tools import create_it_ticket
    return create_it_ticket(employee_id, ticket_type, description)


@app.get("/collections/count")
def count():
    _, store, _, _, _ = build_stack()
    return {"collection": settings.QDRANT_COLLECTION, "points": store.count()}


# ---- PROJECT 2: Kafka streaming ingestion -------------------------------
@app.get("/metrics")
def metrics():
    """Prometheus text exposition of maia_* metrics (spec §11)."""
    from maia.stream.metrics import registry

    return Response(content=registry.render(), media_type="text/plain; version=0.0.4")


@app.post("/ingest/stream")
def ingest_stream():
    """Parse DATA_DIR and produce chunk events to topic.doc.chunks (spec §3)."""
    from maia.stream.producer import ChunkProducer
    from maia.stream.transport import build_default_transport

    transport = build_default_transport()
    producer = ChunkProducer(transport, partitioning=settings.KAFKA_PARTITIONING)
    produced = producer.ingest_dir()
    return {"produced": produced, "mode": producer.mode,
            "topic": settings.KAFKA_TOPIC_CHUNKS}


@app.post("/stream/run-workers")
def run_stream_workers(workers: int = settings.KAFKA_WORKERS):
    """Run embedding workers until drained (embed -> upsert -> commit, spec §5-7)."""
    from maia.embeddings import Embedder
    from maia.stream.store import build_stream_store
    from maia.stream.transport import build_default_transport
    from maia.stream.worker import run_workers

    transport = build_default_transport()
    embedder = Embedder(model=settings.EMBED_MODEL, dim=settings.EMBED_DIM)
    store = build_stream_store(embedder)
    stats = run_workers(workers, transport, store, embedder)
    return {"workers": stats, "consumer_group": settings.KAFKA_CONSUMER_GROUP,
            "lag": transport.lag(settings.KAFKA_CONSUMER_GROUP, settings.KAFKA_TOPIC_CHUNKS)}


@app.get("/stream/lag")
def stream_lag():
    """Consumer lag for the embedding-workers group (spec §9)."""
    from maia.stream.transport import build_default_transport

    transport = build_default_transport()
    return {"consumer_group": settings.KAFKA_CONSUMER_GROUP,
            "topic": settings.KAFKA_TOPIC_CHUNKS,
            "lag": transport.lag(settings.KAFKA_CONSUMER_GROUP, settings.KAFKA_TOPIC_CHUNKS)}
