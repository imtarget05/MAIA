# MAIA realtime streaming closeout — canonical evidence (SSE, `/chat/stream`)

Date (UTC): 2026-09-30
Code commit: `dd39026` — `feat(stream): SSE token streaming for POST /chat/stream`
Parent: `20ec528`. Scope: MAIA only. No WebSocket, no Redis/Kafka, no new service.

## 1. Pre-existing streaming audit (no duplication)

| Path | State before this change |
|---|---|
| `POST /chat/stream` | Bare untyped `data:` frames + `[DONE]`; sync generator; no disconnect handling, no citations event, no error contract; persisted via `chat()` **before** delivery. |
| `POST /agent/chat` + `stream:true` | LangGraph **node** events, not answer tokens. Untouched. |
| `LocalOpenAICompatLLM.chat_stream` (`src/maia/llm.py:137`) | True provider SSE when upstream supports it, honest sentence-chunk fallback otherwise. Reused as-is. |

Verdict: no working token-stream path existed → hardened `/chat/stream` in place on the existing agent/auth/retrieval stack.

## 2. Implementation (`dd39026`: 4 files, +786/−17)

- NEW `src/maia/streaming.py` — typed frames, `split_tokens()`,
  `citations_to_sources()`, refcounted `defer_session_persist()` (per
  tenant:session buffering; concurrent sessions independent; flush only on
  clean completion).
- `POST /chat/stream` rewritten async: auth → rate limit → tenant from auth
  user → `agent.chat()` in `to_thread` under `asyncio.wait_for` →
  `meta → token* → citations → done`, or `approval_required → citations → done`,
  or `error → done`. `done` exactly once; `error` never carries tracebacks.
- HIGH_RISK: zero tool calls on the stream path; `needs_approval` stops the
  action path, pending proposal survives (separate store), execution stays in
  `/actions/confirm` (C1).

## 3. Targeted suites on the final code commit (worktree @ `dd39026`)

- `tests/test_chat_stream_sse.py` → **16 passed** (STREAM-001…015 + no-orphan-threads)
- Adjacent (`test_p1a_stream_ratelimit`, `test_session_registry`, `test_approval`) → **47 passed** (same files, one command)

## 4. Negative controls (source restored + verified clean after each)

| Mutation | Result |
|---|---|
| M1: drop auth dependency from `/chat/stream` | `test_STREAM002` **FAILED** (detected) |
| M2: `is_disconnected` check → `if False:` | `test_STREAM009` + `test_STREAM010` **FAILED** (detected) |

## 5. Latency evidence — orchestration only, NOT for CV

`docs/evidence/stream_latency.json`: 20 fixed prompts, stream_mode=**MOCK**
(stubbed agent; measures SSE delivery overhead, not provider generation):

- TTFT p50 = 2.1 ms, p95 = 97.2 ms (p95 ≈ first-request warmup)
- E2E  p50 = 2.1 ms, p95 = 97.2 ms

Do not publish these numbers as production LLM latency. No claim that
streaming reduces total generation time.

## 6. Full regression + parent-SHA proof (same env, same command `pytest tests/ -q`)

| Tree | Result |
|---|---|
| Clean parent `20ec528` (worktree) | 981 passed, 3 skipped, 3 xfailed, **1 failed** (`test_format_checker_rejects_bad_datetime`) |
| Parent + streaming diff only (worktree) | 997 passed, 3 skipped, 3 xfailed, **1 failed** (same test) |
| Final code commit `dd39026` (worktree) | **997 passed, 3 skipped, 3 xfailed, 1 failed** (same test) |

Residual classification:

- `test_json_schema_engine.py::test_format_checker_rejects_bad_datetime` —
  **PRE-EXISTING / NOT CAUSED BY STREAMING**: fails deterministically on the
  clean parent (installed `jsonschema` version does not enforce `format` without
  an explicit FormatChecker); unmodified test + unmodified module.
- `test_embedder_singleton_reused` — **NOT CAUSED BY STREAMING**: passes on
  parent, parent+streaming, and final commit. It failed only in the main
  worktree, which contains unrelated uncommitted changes (`eval.py`,
  `pipeline_query.py`, `reranker.py`, `test_health_endpoints.py` itself). Out of
  scope; left untouched.

The old `967/7/3 @ 38189ca` figure is retired.

## 7. Verdict table

```text
Feature implementation      VERIFIED (dd39026)
Typed SSE contract          VERIFIED
Auth / tenant isolation     VERIFIED
Approval-safe execution     VERIFIED
Disconnect delivery stop    VERIFIED
Partial-write discard       VERIFIED
Mutation controls           VERIFIED
Mock TTFT evidence          VERIFIED (orchestration only)
Real provider token stream  PARTIAL / provider-dependent (fallback documented)
Compute cancellation        PARTIAL (delivery stops, in-flight sync call drops result)
Full suite                  997/3/3 + 1 PRE-EXISTING failure (proven on parent)
Canonical SHA               dd39026 (code) + docs commit below
```

## 8. Approved CV wording (do not strengthen)

> Implemented authenticated multi-tenant SSE response streaming with grounded
> citations, disconnect-aware delivery cancellation, partial-write discard,
> and approval-safe agent execution.

Token variant (only with the fallback clause):

> Implemented SSE streaming with provider token streaming when available and
> deterministic chunked fallback, preserving tenant isolation, grounded
> citations, and approval-safe execution.

Never claim: WebSocket, lower total latency, scale, or compute cancellation.
