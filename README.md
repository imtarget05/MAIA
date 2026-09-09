# MAIA — Intelligent RAG Knowledge Platform

> A production-style RAG (Retrieval-Augmented Generation) knowledge platform: ingests documents, creates embeddings, retrieves relevant context from a vector database, and grounds LLM responses on retrieved project knowledge — with hybrid retrieval, reranking, and formal evaluation.

## Contract

All agent responses conform to `AgentResponseModel` (`src/maia/agent/schemas.py`). Every response is a typed object — never a bare string.

### ResponseStatus

```yaml
ResponseStatus:
  - answered
  - insufficient_evidence
  - needs_approval
  - needs_clarification
  - action_completed
  - action_cancelled
  - error
```

### Citation

```yaml
Citation:
  tag: str                  # e.g. "[S1]"
  chunk_id: str             # default ""
  filename: str             # default ""
  section: str              # default ""
  page: str                 # default ""
  text: str                 # evidence excerpt (<=600 chars) — first-class provenance
  relevance: Relevance      # "high" | "medium" | "low"  (dense_score thresholds: >=0.65 high, >=0.40 medium)
  dense_score: float        # default 0.0
  fused_score: float        # default 0.0
  rerank_score: float       # default 0.0
```

### EvidenceSummary

```yaml
EvidenceSummary:
  attempts: int             # default 0
  top_dense: float          # default 0.0
  reason: str               # default ""
  final_query: str          # default ""
  rewritten_queries: list[str]  # default []
```

### GroundingInfo

```yaml
GroundingInfo:
  supported: bool           # default False
  score: float              # default 0.0
  cites_valid: bool         # default True
```

### PendingAction (C1 — awaiting human approval)

```yaml
PendingAction:
  type: str                 # e.g. "create_it_ticket" | "create_leave_request"
  params: dict              # default {}
  summary: str              # human-readable card text for the approval UI
```

### ActionResult (side-effect after explicit approval)

```yaml
ActionResult:
  type: str                 # default ""
  status: str               # "completed" | "failed" | "cancelled"  (default "completed")
  result: dict              # default {}
  verify: dict | null       # default null
```

### AgentResponseModel

```yaml
AgentResponseModel:
  status: ResponseStatus            # default "answered"
  answer: str                       # default ""
  intent: str                       # default "general"
  citations: list[Citation]         # default [] — chunks the answer actually cites ([S1]..[Sn], validated)
  retrieved: list[Citation]         # default [] — chunks retrieved but NOT sufficient (insufficient_evidence only)
  evidence: EvidenceSummary         # default EvidenceSummary() — single retrieval packet trace (attempts, top_dense, reason, queries)
  grounding: GroundingInfo          # default GroundingInfo() — {supported, score, cites_valid}
  action: ActionResult | null       # default null — completed side-effect result (only after explicit approval)
  pending_action: PendingAction | null  # default null — proposed side-effect awaiting human approval (C1)
  slots: dict                       # default {} — filled/missing slots for intent
  needs_clarification: bool         # default False
  clarification_question: str | null  # default null
```

### Endpoint Contracts

#### `POST /query` — Single-turn RAG

**Request** (`QueryReq`):
```yaml
question: str
top_k: int | null       # default null → TOP_K_FINAL
tenant_id: str | null   # tenant from auth, never client-chosen
session_id: str | null
```

**Response** (`dict` — from `pipeline_query.query()`):
```yaml
answer: str
citations: list[dict]   # each: {tag, chunk_id, filename, page, section, dense_score, fused_score, rerank_score, text}
has_evidence: bool
candidates: list[dict]
llm_mode: str
rerank_mode: str
refused: bool | null    # present when evidence gate failed
extra: dict | null      # e.g. {top_dense_score} when refused
_trace: dict | null     # present when PIPELINE_TRACE enabled
```

#### `POST /chat` — Agentic RAG

**Request** (`ChatReq`):
```yaml
question: str
session_id: str | null
employee_id: str | null
top_k: int | null
tenant_id: str | null
```

**Response** (`AgentResponseModel`):
```yaml
# Full AgentResponseModel as defined above.
# status ∈ {answered, insufficient_evidence, needs_approval, needs_clarification, action_completed, action_cancelled, error}
# When status == "needs_approval": pending_action is populated, action is null
# When status == "action_completed": action is populated, pending_action is null
# When status == "needs_clarification": clarification_question is populated, needs_clarification == true
```

#### `POST /actions/confirm` — Confirm/Cancel pending action (C1)

**Request** (`ConfirmReq`):
```yaml
session_id: str
employee_id: str | null
approved: bool          # default true
idempotency_key: str | null
```

**Response** (`AgentResponseModel`):
```yaml
# Returns AgentResponseModel with:
#   status == "action_completed" | "action_cancelled"
#   action: ActionResult — the executed side-effect result
#   answer: str — human-readable result summary
```

## Machine-Readable Contract (JSON Schema)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MAIA Agent Response Contract",
  "description": "Machine-readable contract for all MAIA agent responses",
  "definitions": {
    "ResponseStatus": {
      "type": "string",
      "enum": ["answered", "insufficient_evidence", "needs_approval", "needs_clarification", "action_completed", "action_cancelled", "error"]
    },
    "Citation": {
      "type": "object",
      "properties": {
        "tag": {"type": "string", "default": "[S1]"},
        "chunk_id": {"type": "string", "default": ""},
        "filename": {"type": "string", "default": ""},
        "section": {"type": "string", "default": ""},
        "page": {"type": "string", "default": ""},
        "text": {"type": "string", "default": ""},
        "relevance": {"type": "string", "enum": ["high", "medium", "low"], "default": "low"},
        "dense_score": {"type": "number", "default": 0.0},
        "fused_score": {"type": "number", "default": 0.0},
        "rerank_score": {"type": "number", "default": 0.0}
      }
    },
    "EvidenceSummary": {
      "type": "object",
      "properties": {
        "attempts": {"type": "integer", "default": 0},
        "top_dense": {"type": "number", "default": 0.0},
        "reason": {"type": "string", "default": ""},
        "final_query": {"type": "string", "default": ""},
        "rewritten_queries": {"type": "array", "items": {"type": "string"}, "default": []}
      }
    },
    "GroundingInfo": {
      "type": "object",
      "properties": {
        "supported": {"type": "boolean", "default": false},
        "score": {"type": "number", "default": 0.0},
        "cites_valid": {"type": "boolean", "default": true}
      }
    },
    "PendingAction": {
      "type": "object",
      "properties": {
        "type": {"type": "string", "default": ""},
        "params": {"type": "object", "default": {}},
        "summary": {"type": "string", "default": ""}
      }
    },
    "ActionResult": {
      "type": "object",
      "properties": {
        "type": {"type": "string", "default": ""},
        "status": {"type": "string", "enum": ["completed", "failed", "cancelled"], "default": "completed"},
        "result": {"type": "object", "default": {}},
        "verify": {"type": ["object", "null"], "default": null}
      }
    },
    "AgentResponseModel": {
      "type": "object",
      "properties": {
        "status": {"$ref": "#/definitions/ResponseStatus", "default": "answered"},
        "answer": {"type": "string", "default": ""},
        "intent": {"type": "string", "default": "general"},
        "citations": {"type": "array", "items": {"$ref": "#/definitions/Citation"}, "default": []},
        "retrieved": {"type": "array", "items": {"$ref": "#/definitions/Citation"}, "default": []},
        "evidence": {"$ref": "#/definitions/EvidenceSummary", "default": {}},
        "grounding": {"$ref": "#/definitions/GroundingInfo", "default": {}},
        "action": {"$ref": "#/definitions/ActionResult", "default": null},
        "pending_action": {"$ref": "#/definitions/PendingAction", "default": null},
        "slots": {"type": "object", "default": {}},
        "needs_clarification": {"type": "boolean", "default": false},
        "clarification_question": {"type": ["string", "null"], "default": null}
      }
    }
  }
}
```

## Pipeline

```
Documents → Ingestion → Chunking → Embedding → Vector Store (Qdrant)
                                                     ↑ retrieval
User Question → Query Embedding → Hybrid Retrieval (dense + BM25 → RRF)
             → Rerank → Context Assembly → LLM → Grounded Answer + Citations
```

## Kafka Streaming Ingestion (PROJECT 2)

The embedding step is parallelized through Kafka so it scales horizontally:

```
Parser ──> topic.doc.chunks ──> [Embedding Worker x N] ──> Vector DB (Qdrant)
                                    └─> DLQ (failures) + metrics / Prometheus
```

- **Consumer group** `embedding-workers`: 1 partition → 1 consumer; workers scale `1 → 2 → 4 → 8` by re-partitioning.
- **Partitioning modes** (`KAFKA_PARTITIONING`): `ordered` (key=`document_id`, per-doc order, hot-partition risk) or `max-throughput` (key=`hash(doc+chunk)`, parallel). See [`docs/PROJECT_2_STREAMING.md`](docs/PROJECT_2_STREAMING.md).
- **Correct offset ordering**: commit **after** the vector upsert succeeds (§6) → at-least-once + idempotent upsert (`uuid5(chunk_id)`) ⇒ effectively exactly-once, no duplicates on restart.
- **Failures** → inline retries (`KAFKA_MAX_RETRIES`) then `topic.doc.chunks.dlq` + `topic.doc.embedding.failed`.
- **Metrics** (Prometheus): consumer lag, throughput, p95 embedding latency → `GET /metrics` → Grafana dashboard (`grafana/maia-dashboard.json`).

```bash
# Offline (default, no broker needed)
PYTHONPATH=src python -m maia.cli stream produce      # parse → topic.doc.chunks
PYTHONPATH=src python -m maia.cli stream worker 4      # embed → upsert → commit
PYTHONPATH=src python -m maia.benchmark --docs 50 --workers 1,2,4,8
PYTHONPATH=src python -m maia.cli stream metrics       # prometheus text

# Real Kafka via Docker Compose
docker compose up -d kafka kafka-ui qdrant worker     # kafka-ui @ http://localhost:8080
docker compose up -d --scale worker=4                  # scale workers
```

## Architecture

```
src/maia/
├── config.py           # Central settings (pydantic-settings, reads .env)
├── ingestion.py        # Parse & clean .md/.txt/.pdf → RawDoc + metadata
├── chunking.py         # LlamaIndex SentenceSplitter + sliding-window fallback
├── embeddings.py       # FastEmbed sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (384-dim) + hash fallback (MAIA_EMBED_FORCE_HASH=1)
├── vector_store.py     # Qdrant adapter: upsert, dedup, search, delete_by_doc, tenant_id filter
├── retriever.py        # Hybrid: dense (cosine) + BM25 → RRF fusion (k=60), tenant-aware
├── reranker.py         # CrossEncoder ms-marco-MiniLM-L-6-v2 (+ score fallback when torch missing)
├── prompt.py           # Context assembly (cap chars, dedup, [S1] cite markers, boundary tags)
├── llm.py              # Cloudflare Workers AI llama-3.1-8b-instruct (+ MOCK mode when no creds)
├── pipeline_query.py   # Orchestrator: build_stack(), ingest_data_dir(), query()
├── benchmark.py        # Batch benchmark over docs × workers (used in CI smoke)
├── api.py              # FastAPI application layer (query + chat + tools + streaming ingest)
├── cli.py              # CLI entrypoint (ingest/query/chat/health/stream/benchmark)
├── eval.py             # Formal RAG evaluation metrics
├── agent/              # Agentic RAG — Enterprise Employee Assistant (Receptionist)
│   ├── agent.py        # EnterpriseAgent: Decide → Retrieve/Memory/Tool → Evidence → Grounding
│   ├── agentic.py      # AgenticRetriever: iterative retrieval + Memory query rewrite + tenant isolation
│   ├── intents.py      # 10 rule-based intents (+ LLM fallback when mode=cloudflare)
│   ├── tools.py        # Mock HR/IT tools: check_leave_balance / create_leave_request / create_it_ticket
│   ├── hris.py         # Real HRIS connector (HRIS_ENABLED) with mock fallback
│   ├── session.py      # Short-term chat memory (per session_id, MAX_HISTORY_TURNS=8)
│   ├── memory.py       # Long-term memory SQLite cross-session (LTM_ENABLED, default OFF)
│   └── teams.py        # Multi-agent team: researcher→analyst→writer→reviewer (TEAM_ENABLED)
│   └── team/           # Router-delegation team: receptionist routes to scoped member
│       ├── base.py         # AgentRole, AgentMessage, TeamBus, TeamAgent ABC
│       ├── agents.py       # Router + Knowledge/HR/IT scoped agents (C1 never bypassed)
│       └── orchestrator.py # route→delegate, hop guard, soft timeout, team_trace
├── mcp/                # External connectors, MCP-style REST read-only (token-gated)
│   ├── servers.py      # github + notion configs
│   └── client.py       # search/get wrappers, never raise, OFF without tokens
├── voice/              # Voice interface: STT/TTS protocols + audio→chat handler
│   ├── providers.py    # disabled backend ships; real backends plug in later
│   └── handler.py      # VoiceChatHandler (transcribe → agent.chat → optional TTS)
├── finetune/           # Fine-tune groundwork: golden→triplet export (offline) + GPU train runner
│   ├── export.py       # (anchor, positive, negative) JSONL for sentence-transformers
│   └── train.py        # lazy torch import; actionable error when deps absent
├── loops/              # Production RAG loops (2 pipelines + 5 loops + guardrails + lifecycle)
│   ├── knowledge_loop.py      # refresh / delete / re-index, change detection
│   ├── retrieval_loop.py      # Recall@K, Precision@K, MRR, NDCG, citations
│   ├── answer_loop.py         # Grounding + Citation checker, retry, no-evidence fallback
│   ├── evaluation_loop.py     # Golden dataset, BM25 baseline vs hybrid
│   ├── reliability_loop.py    # retry/backoff/DLQ, alert, lag, p95, worker scaling policy
│   ├── guardrails.py          # Input/output safety + prompt-injection defense (boundary tags)
│   ├── document_lifecycle.py  # Versioning (doc_id, version, embedding/chunking ver), soft-delete
│   └── corrective_rag.py      # CRAG (opt-in): grade CORRECT/INCORRECT/AMBIGUOUS → refine/rewrite → web fallback
└── stream/             # Kafka streaming ingestion (PROJECT 2)
    ├── producer.py     # ChunkProducer: parse DATA_DIR → topic.doc.chunks
    ├── worker.py       # Embedding workers: embed → upsert → commit (commit-after-upsert)
    ├── transport.py    # inmemory (offline) / kafka (confluent-kafka) transport + consumer-group lag
    ├── broker_kafka.py # Real Kafka broker adapter
    ├── store.py        # Stream vector store (idempotent upsert uuid5(chunk_id), skip-embedded)
    ├── events.py       # Chunk / DLQ / embedding.failed event schemas
    └── metrics.py      # Prometheus registry (maia_*): lag, throughput, p95 latency
```

## Agentic RAG — Enterprise Employee Assistant

`EnterpriseAgent` (`src/maia/agent/agent.py`) chạy vòng lặp Decide → Retrieve/Memory/Tool → Evidence → Grounding:

- **Intent:** 10 intents rule-based (`leave_request`, `leave_balance`, `it_help`, `vpn`, `security`, `expense`, `benefits`, `onboarding`, `hr_policy`, `general`) + LLM fallback chỉ khi `llm.mode=cloudflare` (`src/maia/agent/intents.py`).
- **Slots:** `days` + `start_date` cho `leave_request`; thiếu slot → hỏi làm rõ, chưa retrieval (`slots_for_intent`).
- **Tools (mock, lưu ở `storage/hr_mock.json`):** `check_leave_balance` (mặc định 12 ngày), `create_leave_request` (trừ balance, sinh `LV-YYYYMMDD-xxx`, từ chối khi `days > balance` hoặc ngoài 1–30), `create_it_ticket` (prefix `VPN`/`SEC`/`IT` theo loại). HRIS thật qua `HRIS_ENABLED`/`HRIS_BASE_URL`, lỗi → fallback mock (`src/maia/agent/hris.py`).
- **Memory:** rewrite câu hỏi từ lịch sử `session_store` (`MAX_HISTORY_TURNS=8`), tenant isolation theo `tenant_id` payload filter (`src/maia/agent/agentic.py`).
- **Grounding:** `GroundingChecker` + `CitationChecker`; trả `intent`, `citations[]`, `has_evidence`, `grounding_score`, `cites_valid`, `action`, `needs_clarification`.
- **RBAC nhiều tenant:** mọi query/chat/stream đều nhận `tenant_id`, Qdrant filter theo payload `tenant_id` (mặc định `default`).

## Stack

- **Framework:** LlamaIndex core (`SimpleDirectoryReader`, `SentenceSplitter`, `Document`)
- **Embeddings:** HuggingFace sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (384-dim) via **FastEmbed** (ONNX, no torch required)
- **Vector DB:** **Qdrant** — collection `maia_knowledge`, COSINE distance, Docker local
- **Retrieval:** hybrid **dense + BM25** fused with **RRF (k=60)**
- **Rerank:** cross-encoder `ms-marco-MiniLM-L-6-v2` (falls back to RRF order without torch)
- **LLM:** **Cloudflare Workers AI** `@cf/meta/llama-3.1-8b-instruct` — automatic **MOCK mode** when no credentials (works fully offline)
- **API:** FastAPI · **UI:** Streamlit with citation/evidence display

## Quickstart

```bash
cp .env.example .env   # fill CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN (optional — mock works offline)
docker compose up -d qdrant
pip install -r requirements.txt
```

### CLI

```bash
PYTHONPATH=src python -m maia.cli ingest [--enterprise]  # samples (mặc định) | data/enterprise
PYTHONPATH=src python -m maia.cli query "Hybrid search hoạt động thế nào?"
PYTHONPATH=src python -m maia.cli chat "Tôi muốn xin nghỉ phép 5 ngày từ 10/09" [--approve] [--crag]
PYTHONPATH=src python -m maia.cli health
PYTHONPATH=src python -m maia.cli stream produce | stream worker 4 | stream lag | stream metrics
PYTHONPATH=src python -m maia.cli benchmark [n_docs]
```

> Luồng C1: `chat` với side-effect chỉ trả `needs_approval` + `pending_action`
> (không thực hiện). Thêm `--approve` để duyệt ngay trong cùng process,
> hoặc gọi `POST /actions/confirm` khi chạy API server.
> Thêm `--crag` (hoặc `CRAG_ENABLED=true`) để bật grading/refinement cho
> câu hỏi khó — response kèm khối `crag` (`action`/`grades`/`trace`).

### Full demo

```bash
./run_demo.sh   # ingest → query → eval
```

### API

```bash
PYTHONPATH=src uvicorn maia.api:app --reload --port 8000
```

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | System status: `qdrant_points`, `llm_mode`, `rerank_mode`, `embed_model` |
| POST | `/query` | Single-turn RAG: `{ "question": "...", "top_k": 3, "tenant_id": null }` |
| POST | `/chat` | Agentic RAG: `{ "question": "...", "session_id": "...", "employee_id": "emp_001", "top_k": null, "tenant_id": null }` → typed `status` (`answered`/`insufficient_evidence`/`needs_approval`/`needs_clarification`/`action_completed`/`action_cancelled`/`error`) |
| POST | `/actions/confirm` | Duyệt/hủy pending action: `{ "session_id": "...", "employee_id": "emp_001", "approved": true }` — side-effect chỉ chạy tại đây (C1) |
| GET | `/actions/pending/{session_id}` | Xem pending action đang chờ duyệt |
| POST | `/chat/stream` | SSE streaming chat (`data: <chunk>` … `data: [DONE]`) |
| GET | `/chat/history/{session_id}` | Chat history của session |
| DELETE | `/chat/history/{session_id}` | Xóa history của session |
| GET | `/tools/leave/balance?employee_id=emp_001` | Số ngày phép + danh sách requests (mock/HRIS) |
| POST | `/tools/leave/request?employee_id=..&days=..&start_date=..` | Tạo đơn nghỉ phép |
| POST | `/tools/it/ticket?employee_id=..&ticket_type=..&description=..` | Tạo IT ticket (`vpn_request`/`lost_device`/`laptop_broken`/`general`) |
| POST | `/memory/store` | Lưu preference/fact LTM: `{ "user_id": "...", "type": "preference", "content": "..." }` (cần `LTM_ENABLED`) |
| GET | `/memory/recall?user_id=..&q=..&k=5` | Recall top-k LTM theo token overlap (offline) |
| DELETE | `/memory/{id}?user_id=..` | Quên 1 memory |
| POST | `/teams/run` | Chạy team researcher→analyst→writer→reviewer: `{ "question": "...", "members": null }` (cần `TEAM_ENABLED`; CLI: `teams <q>`) |
| POST | `/team/chat` | Router-delegation team: `{ "question": "...", "session_id": "...", "employee_id": "..." }` → typed response + `member`/`route`/`team_trace` (cần `TEAM_ENABLED`; CLI: `team chat <q>`) |
| GET | `/mcp/status` | Trạng thái connectors (không lộ secret) |
| GET | `/mcp/github/search?q=..` · `/mcp/notion/search?q=..` | Search read-only ngoài (cần token, không thì `connector_disabled`) |
| POST | `/voice/chat` | Audio → transcript → `agent.chat` (+TTS nếu `speak=true`); cần `VOICE_ENABLED` |
| POST | `/finetune/export` | Export triplets `(anchor, positive, negative)` từ goldens + corpus live |
| GET | `/finetune/deps` | Kiểm tra deps train GPU (`torch`, `sentence-transformers`, `datasets`) |
| POST | `/ingest` | Ingest `DATA_DIR` (mặc định `data/samples`), query param `tenant_id` |
| POST | `/ingest/enterprise` | Ingest `ENTERPRISE_DATA_DIR` (`data/enterprise`), query param `tenant_id` |
| POST | `/ingest/upload` | Upload và ingest files (.md/.txt/.pdf) |
| POST | `/ingest/url` | Notebook-style: fetch 1 trang web/PDF vào sources của session: `{ "url": "...", "session_id": "...", "tenant_id": null }` |
| GET | `/sources?session_id=..` | List link sources của session (mới nhất trước) |
| DELETE | `/sources/{doc_id}?session_id=..` | Xóa 1 link source (verify ownership) |
| GET | `/collections/count` | Points trong collection |
| POST | `/ingest/stream` | Parse `DATA_DIR` → produce chunks tới `topic.doc.chunks` |
| POST | `/stream/run-workers?workers=n` | Chạy `n` embedding workers (embed→upsert→commit) |
| GET | `/stream/lag` | Consumer lag của group `embedding-workers` |
| GET | `/metrics` | Prometheus text (`maia_*` metrics) |

### UI (Enterprise chat)

```bash
streamlit run app_streamlit.py   # mặc định :8501; nếu kẹt port dùng --server.port 8502
```

`app_streamlit.py` là chat UI 4 state theo đúng typed contract backend — **Find → Understand → Cite → Act**:

| State | Hiển thị |
|---|---|
| 🟢 `answered` | Câu trả lời + panel Evidence (`[Sn] filename · section`, quote bằng chứng, nhãn `Highly relevant/Supporting/Weak`) |
| 🔴 `insufficient_evidence` | Từ chối trung thực + số đoạn retrieved-nhưng-yếu + gợi ý diễn đạt lại + liên hệ con người (không suy đoán) |
| 🔵 `needs_approval` | Thẻ duyệt action (summary + params) với nút **Create/Cancel** — side-effect chỉ chạy khi bấm Create (C1) |
| ✅ `action_completed` | Thẻ kết quả (mã ticket/request, status, assignee, verified) kèm citations |

Mọi tin nhắn đều có caption trạng thái (`intent` · số sources · grounding), expander
“How MAIA answered” (attempts, gate reason, final query) và expander Debug
(raw dense/fused/rerank, chunk_id — chỉ dành cho dev). Thêm: lịch sử theo
`session_id` (+ `tenant_id`, `employee_id`), 4 nút demo, re-ingest samples/enterprise,
upload file, panel **🔗 Link sources** (dán URL trang web/PDF làm nguồn riêng
của session — tra cứu cite đúng link, session khác không thấy), mục
“Legacy: Single-turn RAG query”.

## Evaluation

```bash
PYTHONPATH=src python -m maia.eval eval/dataset.jsonl                     # RAG cơ bản (6 câu)
PYTHONPATH=src python -m maia.eval --agent eval/enterprise_dataset.jsonl  # intent enterprise (10 câu)
PYTHONPATH=src python -m maia.loops.evaluation_loop eval/retrieval_dataset.jsonl  # BM25 baseline vs hybrid (5 câu, chạy trong CI)
```

Metrics: **hit@k**, **recall@k**, **context precision**, **faithfulness proxy**, **answer relevance proxy** (+ MRR/NDCG trong retrieval loop).
Dataset format (JSONL): `{"question": str, "gold_chunk_ids": [...], "gold_keywords": [...]}` (enterprise thêm `intent`, `expected_action`).

Baseline embedding hiện tại (`eval/baseline_bge-small-en-v1.5.json`, chốt trước mọi đổi model — hard rule C2):
RAG `hit@k=1.0`, `recall@k=1.0`, `context_precision=0.764` · intent enterprise `10/10` ·
retrieval hybrid vs BM25: `recall@k +0.0`, `mrr@k +0.05`, `ndcg@k +0.0246` (Qdrant 11 points, dim 384, rerank fallback).

## Case Study — MAIA internal Q&A / policy assistant with PII redaction (MAIA-08)

> Trợ lý nội bộ trả lời dựa trên tài liệu công ty, bắt buộc redact PII trước khi trả lời.
> Internal assistant answering from company docs, with mandatory PII redaction before answering.

**Problem / Vấn đề.**
Một trợ lý nội bộ phải redact email/thông tin cá nhân trước khi trả lời — nhưng redact quá tay phá hỏng chính câu trả lời hữu ích. Ví dụ: "cần hỗ trợ VPN thì liên hệ ai?" mà email liên hệ bị thay bằng `[PII-EMAIL]` thì câu trả lời vô dụng.
An internal assistant must redact personal emails/PII before answering — but over-redaction destroys useful answers. Example: "who do I contact for VPN help?" is useless if the contact email becomes `[PII-EMAIL]`.

**Architecture / Kiến trúc.**
Hybrid retrieval: dense Qdrant cosine (`TOP_K_DENSE=10`) + sparse `BM25Okapi` (`TOP_K_BM25=10`) → RRF fusion (`RRF_K=60`, `TOP_K_FUSED=8`) → cross-encoder rerank `ms-marco-MiniLM-L-6-v2` (`TOP_K_FINAL=3`) — `src/maia/retriever.py:1-9`, `src/maia/reranker.py:11`, `src/maia/config.py:16-21`. Embedding: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 384-dim via FastEmbed ONNX + hash fallback (`MAIA_EMBED_FORCE_HASH=1`) — `src/maia/embeddings.py:33,43-49`. PII scanner 2 lớp: ingest-time (`pipeline_query.py:54-73`) + output guardrail (`loops/guardrails.py:93-114`), patterns tại `loops/pii.py:21-34`. Golden set đánh giá cả retrieval lẫn answer — `src/maia/eval.py:78-125`.

**Investigation & finding — Diagnostic PII scanner (MAIA-08).**
Ban đầu nghi ngờ scanner false-positive cao. Diagnostic xác nhận: đó không phải lỗi pattern — email liên hệ thật vẫn bị flag, và recall không bị ảnh hưởng (retrieval vẫn đúng chunk). Phát hiện quan trọng hơn nằm ở tầng đánh giá: `recall@k` chỉ đo "có tìm đúng chunk không", không đo "câu trả lời cuối còn dùng được không". Câu hỏi "liên hệ ai" vẫn retrieve đúng chunk, nhưng email trong đó bị redact thành placeholder → đúng chunk, sai nội dung — metric hiện tại không bắt được.
Initially suspected high false-positive rate. Diagnostic confirmed: not a pattern bug — real contact emails are flagged, retrieval still finds the right chunk. The deeper finding is a measurement gap: `recall@k` measures "right chunk retrieved", not "final answer still usable". A "who to contact" question retrieves correctly but the answer is useless after redaction — right chunk, wrong content — invisible to current metrics.
Verified: `PIIScanner` dùng 1 regex `_EMAIL` duy nhất, không allowlist (`loops/pii.py:27`) → 7/7 role emails (`support@`, `it-help@`, `hr@`, `security@`, `benefits@`, `eap@`, `onboarding@`) đều bị redact thành `[PII-EMAIL]` (repro bằng `PIIScanner.redact`). `tests/test_pii.py:17,68,76-79` thậm chí assert `hr@company.com` là PII phải redact.

**Fix / follow-up (G-04-FU2 — committed, xem `docs/G-04-FU2_PII_ROLE_ALLOWLIST.md`).**
Phân biệt email cá nhân (luôn redact) với email vai trò/phòng ban nội bộ (`support@`, `it-*@`, `hr@`, `security@`, ...) — nhóm sau không redact. Thêm golden split `eval/golden/contact_usability.jsonl` kiểm tra riêng: sau redact, câu trả lời "liên hệ ai" vẫn phải chứa contact dùng được + metric `contact_usability_rate` bên cạnh `recall@k`.
Distinguish personal emails (always redact) from internal role/department emails (never redact). Add a dedicated golden split + `contact_usability_rate` metric alongside `recall@k`.
Đây là câu chuyện đáng kể nhất để show cho nhà tuyển dụng — không phải "tìm bug", mà là phát hiện khoảng trống trong chính cách đo lường chất lượng (metric nhìn ổn nhưng sản phẩm thực ra hỏng).
This is the strongest hiring story — not "found a bug" but "found a gap in how quality itself is measured" (metrics look green while the product is broken).

**Test coverage / metrics (số thật, measured 2026-09-07).**
Golden set: **73 cases / 9 splits** (`eval/golden/`: `vi_policy 15`, `en_policy 10`, `exact 8`, `paraphrase 8`, `tool_request 8`, `ambiguous 6`, `injection 6`, `no_answer 6`, `unauthorized 6`).

| Split | n | hit@k | recall@k | ctx_prec | faith | mrr |
|---|---|---|---|---|---|---|
| vi_policy | 15 | 0.400 | 0.367 | 0.378 | 0.400 | 0.367 |
| en_policy | 10 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| exact | 8 | 0.250 | 0.188 | 0.188 | 0.250 | 0.188 |
| paraphrase | 8 | 0.125 | 0.125 | 0.292 | 0.500 | 0.042 |
| tool_request | 8 | 0.250 | 0.250 | 0.188 | 0.375 | 0.083 |
| ambiguous | 6 | 0.333 | 0.333 | 1.000 | 0.333 | 0.000 |
| injection | 6 | 0.000 | 0.000 | 1.000 | 0.000 | 0.000 |
| no_answer | 6 | 0.000 | 0.000 | 1.000 | 0.000 | 0.000 |
| unauthorized | 6 | 0.167 | 0.167 | 1.000 | 0.167 | 0.000 |
| **micro-avg** | **73** | **0.192** | **0.178** | — | — | — |

Điều kiện đo: `MAIA_EMBED_FORCE_HASH=1` (hash fallback, FastEmbed broken trong env này) + reranker score-fallback (thiếu `sentence-transformers`) + `TOP_K_FINAL=3`, lệnh `MAIA_EMBED_FORCE_HASH=1 PYTHONPATH=src python -m maia.eval --all --top-k 3` (`src/maia/eval.py:131-136`). Đây là **lower bound offline** — baseline FastEmbed thật (`eval/baseline_bge-small-en-v1.5.json`, Qdrant 11 points) đạt RAG `hit@k=1.0/recall@k=1.0/ctx_prec=0.764` và intent `10/10`. Audit metrics sẵn có: `false_refusal_rate`, `refusal_accuracy` (`no_answer 1.0`), `leakage_rate` (`injection 0.0`, `unauthorized 0.167`) — nhưng chưa có metric nào đo "contact còn dùng được sau redact" (chính là G-04-FU2).

**What I'd do next.**
Hoàn thành G-04-FU2 theo spec trong `docs/G-04-FU2_PII_ROLE_ALLOWLIST.md`: (1) role-email allowlist + unit test, (2) golden split `contact_usability` mới, (3) metric `contact_usability_rate` chạy cùng `evaluate_all`. Sau đó re-run eval ở cả 2 mode (hash + FastEmbed) để chứng minh recall giữ nguyên trong khi usability tăng.

## Tests

```bash
python -m pytest tests/ -q   # 128 passed, offline hoàn toàn (MAIA_EMBED_FORCE_HASH=1, không cần Qdrant/Kafka)
```

| File | Số test | Phạm vi |
|---|---|---|
| `tests/test_agent.py` | 11 | intent/slots, mock leave balance, session store, IT ticket, clarification |
| `tests/test_agentic.py` | 6 | memory rewrite, tenant isolation, evidence check + iterative retrieval, HRIS fallback, tool chain mất laptop |
| `tests/test_chunk.py` | 2 | chunk metadata, assemble cap |
| `tests/test_document_lifecycle.py` | 7 | upload v1 manifest, bump version, soft-delete, embedding-model versioning |
| `tests/test_guardrails.py` | 9 | input/output guardrail, injection detect, sanitizer, boundary tags |
| `tests/test_loops.py` | 14 | knowledge CRUD, rank metrics, citation/grounding checker, guarded generate, baseline hybrid vs BM25, alert, scaling policy, health report |
| `tests/test_stream.py` | 10 | ordered partition, drain-to-zero-lag, exactly-once, rebalance, DLQ sau retries, p95 metrics, throughput, routing |
| `tests/test_approval.py` | 9 | evidence gate (no-LLM khi thiếu evidence), propose/cancel/confirm, taxonomy mất-vs-hỏng, topical_low, typed contract |
| `tests/test_corrective_rag.py` | 11 | CRAG grading (heuristic + LLM), refiner, web-fallback OFF, exhausted path, metrics, enabled/disabled wiring |
| `tests/test_memory.py` | 7 | extract/store/recall/forget/tenant-isolation/dedupe, no-op khi tắt |
| `tests/test_teams.py` | 3 | team completed + citations, refused khi rỗng, custom members |
| `tests/test_team.py` | 10 | routing hr/it/knowledge, delegation, hop guard, C1 không bypass, fallback, timeout |
| `tests/test_mcp.py` | 6 | status không lộ secret, disabled mặc định, GitHub/Notion mocked (search + auth-fail) |
| `tests/test_voice.py` | 5 | providers disabled, roundtrip fake STT/TTS (±speak), lỗi STT lan truyền |
| `tests/test_finetune.py` | 5 | build/export triplets JSONL, đọc goldens repo, dep guard |
| `tests/test_url_ingest.py` | 13 | validate_url (SSRF/localhost/userinfo), dedup, session isolation, delete ownership, cap, agent cite link |

CI (`.github/workflows/ci.yml`, matrix Python 3.11/3.12): pytest offline + `maia.benchmark --docs 20 --workers 1,2,4 --partitions 8` smoke + `evaluation_loop` trên `retrieval_dataset.jsonl` + job dryrun (compileall, import stream modules không cần `confluent-kafka`, validate `grafana/maia-dashboard.json`).

## Features

- [x] Document ingestion, chunking, embedding, vector retrieval
- [x] URL sources notebook-style (dán link web/PDF → nguồn riêng theo session, trafilatura + SSRF guard, dedup, citation đúng link) — `fetch_url` (`ingestion.py`), `POST /ingest/url`, `GET/DELETE /sources`
- [x] Hybrid retrieval (dense + BM25 + RRF) — `src/maia/retriever.py`
- [x] Reranking (CrossEncoder + score fallback khi thiếu torch) — `src/maia/reranker.py`
- [x] Context assembly with dedup + char cap + `[S1]` citation markers + boundary tags — `src/maia/prompt.py`
- [x] Grounded answering with explicit "no evidence" guard — `src/maia/loops/answer_loop.py`
- [x] Formal RAG evaluation — `src/maia/eval.py` + `eval/dataset.jsonl` (6 câu), `--agent` + `eval/enterprise_dataset.jsonl` (10 câu), `evaluation_loop` + `eval/retrieval_dataset.jsonl` (5 câu)
- [x] Citation/evidence display — `citations[]` in API + Streamlit expanders
- [x] **Agentic RAG — Enterprise Employee Assistant** — `src/maia/agent/` (Decide → Retrieve/Memory/Tool → Evidence → Grounding, 10 intents, slots, clarification)
- [x] Typed response contract (`status`/`citations`/`retrieved`/`evidence`/`grounding`/`action`/`pending_action`) — `src/maia/agent/schemas.py`; một retrieval packet nuôi cả answer lẫn citations
- [x] Confirm-before-action (C1): side-effect chỉ chạy ở `POST /actions/confirm`, mất laptop → `lost_device` (không bao giờ nhầm `laptop_broken`)
- [x] Tool calling: `check_leave_balance` / `create_leave_request` / `create_it_ticket` + HRIS thật (fallback mock) — `src/maia/agent/tools.py`, `src/maia/agent/hris.py`
- [x] Chat memory theo `session_id` + SSE `/chat/stream` + history API — `src/maia/agent/session.py`, `src/maia/api.py`
- [x] Long-term memory SQLite cross-session (`user_id`+`tenant_id`, recall offline, extract tự động khi bật) — `src/maia/agent/memory.py`, `LTM_ENABLED=false` mặc định
- [x] Multi-agent team researcher→analyst→writer→reviewer (1 retrieval packet, 1 revision round) — `src/maia/agent/teams.py`, `POST /teams/run`, CLI `teams`
- [x] Router-delegation team (receptionist → Knowledge/HR/IT scoped agents, hop guard, timeout, `team_trace`, C1 không bypass) — `src/maia/agent/team/`, `POST /team/chat`, CLI `team chat`
- [x] External connectors MCP-style read-only GitHub/Notion (REST, không dep mới, không raise) — `src/maia/mcp/`, `GET /mcp/*`
- [x] Voice interface (STT/TTS protocols + `VoiceChatHandler`, backend disabled sẵn) — `src/maia/voice/`, `POST /voice/chat`
- [x] Fine-tune groundwork (golden→triplet export offline + train runner lazy-dep) — `src/maia/finetune/`, `POST /finetune/export`
- [x] Multi-tenant RBAC qua `tenant_id` payload filter (Qdrant) — query/chat/stream/ingest đều hỗ trợ
- [x] Production loops: knowledge / retrieval-quality (Recall@K, MRR, NDCG) / answer-quality / evaluation / reliability + guardrails (injection defense) + document lifecycle (versioning, soft-delete) + **CRAG opt-in** (grade → refine/rewrite → web fallback, `CRAG_ENABLED=false` mặc định) — `src/maia/loops/`
- [x] **Kafka streaming ingestion (PROJECT 2)** — chunk events, consumer group, parallel workers — `src/maia/stream/`, `docs/PROJECT_2_STREAMING.md`
- [x] Idempotent vector upsert (`uuid5(chunk_id)`, `KAFKA_SKIP_EMBEDDED`) + commit-after-upsert
- [x] Failed-chunk → DLQ + `embedding.failed` event sau `KAFKA_MAX_RETRIES` retries
- [x] Consumer lag, throughput, p95 latency metrics (`GET /metrics`) + Grafana dashboard (`grafana/maia-dashboard.json`)
- [x] Batch benchmark (`maia.benchmark`, Docker Compose Kafka, integration tests) + CI benchmark smoke
- [x] Offline-first tests: 128 passed, không cần Qdrant/Kafka (`MAIA_EMBED_FORCE_HASH=1`)

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `QDRANT_URL` / `QDRANT_COLLECTION` / `QDRANT_API_KEY` | `http://localhost:6333` / `maia_knowledge` / — | Qdrant endpoint (API key chỉ cần cho Qdrant Cloud) |
| `EMBED_MODEL` / `EMBED_DIM` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` / `384` | FastEmbed model; test ép hash mode qua `MAIA_EMBED_FORCE_HASH=1` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 512 / 50 | Chunking params |
| `TOP_K_DENSE/BM25/FUSED/FINAL` | 10/10/8/3 | Retrieval depth |
| `SIMILARITY_THRESHOLD` | 0.3 | Evidence guard on dense score |
| `RRF_K` | 60 | Reciprocal Rank Fusion constant |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_MODEL` | — / — / `@cf/meta/llama-3.1-8b-instruct` | Workers AI creds (trống → MOCK mode) |
| `DATA_DIR` / `ENTERPRISE_DATA_DIR` / `STORAGE_DIR` | `./data/samples` / `./data/enterprise` / `./storage` | Nguồn ingest + cache (BM25, mock HR DB) |
| `TENANT_ID` / `DEFAULT_EMPLOYEE_ID` / `MAX_HISTORY_TURNS` | `default` / `emp_001` / `8` | Multi-tenant + chat memory |
| `AGENT_MAX_ITER` / `AGENT_EVIDENCE_THRESHOLD` / `AGENT_GROUNDING_THRESHOLD` | `3` / `0.3` / `0.15` | Vòng lặp retrieval + ngưỡng grounding |
| `HR_MOCK_DB_PATH` | `./storage/hr_mock.json` | Mock HR DB (leave balance/requests) |
| `HRIS_ENABLED` / `HRIS_BASE_URL` / `HRIS_API_KEY` / `HRIS_TIMEOUT_SEC` | `false` / — / — / `5` | Connector HRIS thật (lỗi → fallback mock) |
| `URL_FETCH_TIMEOUT` / `URL_MAX_BYTES` / `URL_MAX_LINKS_PER_SESSION` | `10` / `2000000` / `20` | Link sources: timeout fetch, cap dung lượng, cap số link/session |
| `STREAM_TRANSPORT` | `inmemory` | `inmemory` (offline) or `kafka` (real broker) |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker address |
| `KAFKA_TOPIC_CHUNKS` / `KAFKA_TOPIC_DLQ` / `KAFKA_TOPIC_FAILED` | `topic.doc.chunks` / `topic.doc.chunks.dlq` / `topic.doc.embedding.failed` | Tên topic |
| `KAFKA_NUM_PARTITIONS` | `4` | Partition count cho `topic.doc.chunks` |
| `KAFKA_CONSUMER_GROUP` | `embedding-workers` | Consumer group id |
| `KAFKA_PARTITIONING` | `ordered` | `ordered` hoặc `max-throughput` (see docs §4) |
| `KAFKA_WORKERS` | `2` | Default worker count |
| `KAFKA_MAX_RETRIES` / `KAFKA_RETRY_BACKOFF_MS` | `3` / `250` | Retry policy trước DLQ |
| `KAFKA_SKIP_EMBEDDED` | `true` | Bỏ qua chunk đã có vector (idempotency) |
| `CRAG_ENABLED` / `CRAG_GRADE_USE_LLM` | `false` / `false` | Bật CRAG grading (opt-in; CLI: `chat <q> --crag`) |
| `CRAG_GRADE_THRESHOLD` / `CRAG_MAX_CORRECTIONS` | `0.35` / `2` | Ngưỡng grade + số lần rewrite/refine tối đa |
| `CRAG_WEB_SEARCH_ENABLED` | `false` | Web fallback khi kiệt corrections (mặc định OFF — enterprise refuse thay vì lên mạng) |
| `LTM_ENABLED` / `LTM_DB_PATH` / `LTM_RECALL_K` | `false` / `./storage/ltm.db` / `3` | Long-term memory cross-session (SQLite) |
| `TEAM_ENABLED` / `TEAM_MAX_REVISION_ROUNDS` | `false` / `1` | Multi-agent team (`POST /teams/run`, CLI `teams`) |
| `TEAM_MAX_HOPS` / `TEAM_TIMEOUT_SEC` | `3` / `30.0` | Router-delegation team (`POST /team/chat`, CLI `team chat`): hop budget + soft timeout |
| `MCP_GITHUB_ENABLED` / `MCP_GITHUB_TOKEN` | `false` / — | Connector GitHub read-only (fine-grained PAT) |
| `MCP_NOTION_ENABLED` / `MCP_NOTION_TOKEN` / `MCP_TIMEOUT_SEC` | `false` / — / `10` | Connector Notion read-only (integration token) |
| `VOICE_ENABLED` / `VOICE_STT_PROVIDER` / `VOICE_TTS_PROVIDER` | `false` / `disabled` / `disabled` | Voice interface (backend thật cắm sau) |
| `FINETUNE_OUTPUT_DIR` | `./storage/finetune` | Nơi ghi triplets export |

## Deployment (all free tier)

| Component | Platform | Notes |
|---|---|---|
| Vector DB | [Qdrant Cloud](https://cloud.qdrant.io) — free 1GB cluster | Copy cluster URL + API key into `QDRANT_URL` / `QDRANT_API_KEY` |
| API (FastAPI) | [Render](https://render.com) — free web service | Blueprint included (`render.yaml`); sleeps after 15 min idle, wakes on request |
| UI (Streamlit) | [Streamlit Community Cloud](https://share.streamlit.io) — always-on free | Configure via App → Settings → Secrets (see `.streamlit/secrets.toml.example`) |
| LLM | Cloudflare Workers AI — free tier | Existing `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN`; empty → MOCK mode |

### Deploy steps

1. **Qdrant Cloud:** create free cluster → get URL + API key.
2. **Render:** New → Blueprint → select repo → fill `QDRANT_URL`, `QDRANT_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` when prompted → Deploy. Health check: `GET /health`.
3. **Streamlit Cloud:** New app → select repo → main file `app_streamlit.py` → in Secrets paste the keys from `.streamlit/secrets.toml.example` with real values.
4. **First ingestion:** `POST /ingest` on the Render API URL, or run `python -m maia.cli ingest` locally pointed at Qdrant Cloud (`.env` with the same `QDRANT_URL`/`QDRANT_API_KEY`) — data persists in Qdrant Cloud, so any later restart is stateless-safe.

> **Free-tier note:** Render's filesystem is ephemeral — the BM25 cache (`storage/bm25_corpus.pkl`) is rebuilt automatically from Qdrant on boot (`retriever._load_or_rebuild()`), so no data loss. Ingested uploads land in Qdrant Cloud and survive restarts. Both UI and API are stateless; scale-to-zero only costs a cold-start.

## Cleanup policy

Safe to delete (regenerated automatically): `storage/` (BM25 cache, mock HR DB), `qdrant_data/`, `__pycache__/`, `.pytest_cache/` — all covered by `.gitignore`.

Never delete: `src/maia/` core modules, `eval/*.jsonl`, `data/samples/`, `data/enterprise/`, `requirements.txt`.

## Sample documents

- `data/samples/` — 3 markdown docs (platform overview, vectors & hybrid search, RAG failure modes) nên query được ngay sau `ingest`.
- `data/enterprise/` — 8 docs (HR_Policy, Leave_Policy, IT_Handbook, IT_Security_Policy_v4.2, VPN_Guide, Expense_Policy, Benefits, Onboarding_Guide) dùng cho Enterprise Assistant (`ingest --enterprise` hoặc `POST /ingest/enterprise`).
