# MAIA — Claim Matrix (canonical truth source)

Every capability MAIA claims, with the evidence that backs it and the limit that
stays attached. If a statement is not in this table, it is not a claim this
repository makes.

Status vocabulary, weakest to strongest:

| Status | Means |
|---|---|
| `NOT_VERIFIED` | designed or discussed, no evidence |
| `IMPLEMENTED` | code exists, not exercised |
| `IMPLEMENTED_TESTED` | automated tests pass (local, mocked deps) |
| `LOCAL_RUNTIME_VERIFIED` | ran as real processes against real local infrastructure |
| `AZURE_VERIFIED` | ran on the deployed Azure runtime, HTTP observed |
| `CLOSED_AS_KNOWN_LIMITATION` | measured defect, deliberately not fixed |
| `N/A` | not claimed |

| Capability | Status | Best evidence | Limitation |
|---|---|---|---|
| Hybrid RAG (dense + BM25 + RRF k=60) | `IMPLEMENTED_TESTED` | `tests/test_llama_index_dataplane.py::test_hybrid_retrieval_with_bm25_and_rrf` | `LOCAL_RUNTIME_VERIFIED` on Azure for a single grounded query; not load-tested |
| Cross-encoder reranker | `IMPLEMENTED_TESTED` | `tests/test_reranker_lifecycle.py`; CI job `reranker` | Falls back to RRF order when weights unavailable; fallback path is the tested default |
| Tenant isolation | `IMPLEMENTED_TESTED` | `tests/test_tenant_authorization.py`, `tests/test_security_audit.py`; Gate 8B-C B8B5 refuses cross-tenant retrieval | Enforced at Qdrant payload + DB filter; not a separate trust boundary |
| MCP tools | `IMPLEMENTED_TESTED` | `tests/test_mcp_*.py`; CI job `promptops-mcp`; 3 MCP paths live in deployed `openapi.json` | Live paths observed, not exercised end-to-end on Azure |
| HITL approval (LangGraph interrupt) | `AZURE_VERIFIED` | Live `POST /agent/chat` → `status=needs_approval` + `pending_action`; `docs/evidence/m6-live-probe.sh` | Two disjoint approval planes (`/agent/chat` resume vs legacy `/actions/confirm`) — documented in `api.py` |
| Durable checkpoint (PostgreSQL) | `AZURE_VERIFIED` | Local: 2 real OS processes (`tests/test_served_path_durability.py`). Azure: `checkpoint_blobs` + per-thread `__interrupt__` writes in PostgreSQL 16.15 | `AZURE_MULTI_REPLICA = NOT_VERIFIED` (deployed `max-replicas=1`). The local two-process proof is the durability authority |
| Idempotency (single side effect) | `AZURE_VERIFIED` | Live replay returned the **same** `request_id`, `duplicate_side_effect=False`; `tests/test_idempotency.py` | **idempotent-once for tested scenarios, NOT exactly-once.** Crash between external mutation and the idempotency write is uncovered. HR mock is JSON, so the write is not transactional with the tool call |
| Provider reliability | `IMPLEMENTED_TESTED` | `tests/test_reliability_classification.py`, `tests/test_resilience.py` (M4: permanent HTTP failures not retried) | Not exercised against a real provider outage |
| SSE transport | `IMPLEMENTED_TESTED` | `tests/test_sse_streaming_contract.py` (9 passed); `docs/evidence/MAIA_SSE_STREAMING.md` | **Not token-level.** First byte after generation completes; TTFT ≠ model latency |
| Redis | `IMPLEMENTED` | `config.py` `REDIS_URL` | Unset by default ⇒ process-local rate limiter, per-replica counters. **Not a durable checkpoint store** |
| Azure OIDC (secretless) | `AZURE_VERIFIED` | Run `37029209709`: positive OIDC `VERIFIED_LIVE`, negative control rejected `AADSTS700213`. `passwordCredentials=0`, `keyCredentials=0` | Only `azure-verify.yml` pins actions to SHAs; other workflows still use tags |
| Azure application runtime | `AZURE_VERIFIED` | `ca-maia-api` revision `--m6c`, image `e119f0fd`: health 200, auth 201/200/200, HITL interrupt + resume, RAG with 2 citations | `VERIFIED_TRANSIENT` — validation topology is `max-replicas=1`, torn down after proof |
| Abstention / evidence gate | `CLOSED_AS_KNOWN_LIMITATION` | `docs/adr/0006-*`; Gate 8B-C 9/11, `max(no-answer)=0.6957 ≥ min(answerable)=0.3139`; four cheap signals measured, none separable | Retrieval score proves topical relevance, **not answerability**. Best cheap signal rejects 36/85 genuine answers. No hallucination-prevention claim is made |
| Observability | `IMPLEMENTED_TESTED` | `tests/test_tracing.py`; container-app structured logs used during M6 | Not a metrics/tracing platform; systemd/MCP audit JSONL only |

## Claims deliberately NOT made

- "Prevents hallucination" — the abstention gap above is measured and open.
- "Exactly-once side effects" — see the idempotency row.
- "Multi-replica durable state on Azure" — proven locally across two processes,
  not on Azure with more than one replica.
- "Token-level streaming" — SSE is incremental, not token-level.
- "Production-ready" — no such claim appears in this repository.
