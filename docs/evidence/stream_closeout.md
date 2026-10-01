# MAIA realtime streaming closeout — canonical evidence (SSE, `/chat/stream`)

Date (UTC): 2026-09-30
Code commit: `dd39026` — `feat(stream): SSE token streaming for POST /chat/stream`
Canonical CI-backed commit: `2d2eaf9` (streaming + CI triage fixes; `dd39026` is historical)
Parent: `20ec528`. Scope: MAIA only. No WebSocket, no Redis/Kafka, no new service.

## 1. Pre-existing streaming audit (no duplication)

| Path | State before this change |
|---|---|
| `POST /chat/stream` | Bare untyped `data:` frames + `[DONE]`; sync generator; no disconnect handling, no citations event, no error contract; persisted via `chat()` **before** delivery. |
| `POST /agent/chat` + `stream:true` | LangGraph **node** events, not answer tokens. Untouched. |
| `LocalOpenAICompatLLM.chat_stream` (`src/maia/llm.py:137`) | True provider SSE when upstream supports it, honest sentence-chunk fallback otherwise. Reused as-is. |

Verdict: no working token-stream path existed → hardened `/chat/stream` in place on the existing agent/auth/retrieval stack.

## 2. Implementation (`dd39026`: 4 files, +786/−17 — historical; canonical is `a82f24b` per §9)

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

## 3. Targeted suites (historical worktree runs at `dd39026`; CI re-verified everything on `a82f24b`, §9)

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
| Parent + streaming diff only (worktree) | 997 passed*, 3 skipped, 3 xfailed, **1 failed** (same test) |
| `dd39026` (worktree) | 997 passed*, 3 skipped, 3 xfailed, 1 failed (same test) |
| **CI on `2d2eaf9` (run `36768831367`)** | **GREEN 14/14 jobs** — unit-tests job: **987 passed, 12 skipped, 2 deselected, 3 xfailed, 0 failed** |

\* `997` was a dirty-worktree figure (two untracked foreign test files present).
Clean `dd39026`/`a80e330` trees collect 992 nodes (983 passed + skips/xfails,
independently reproduced). The CI unit-tests job on `2d2eaf9` is the canonical
count: 987 passed (different skip profile: 12 env-dependent skips, 2
live/infra deselected), 0 failed.

CI triage on the canonical push (failures were all drift, fixed minimally):
`e566cef` (ruff 18 + stdlib date-time check), `a7319ac` (mock.patch test seam),
`482ac30` + `89ef655` (pyright 16, typing-only), `2d2eaf9`
(torch ignores + CI `PYTHONPATH`). The former single residual
(`test_format_checker_rejects_bad_datetime`) is FIXED, not waived: the
installed `jsonschema>=4.24` ships no `date-time` checker, so the adapter now
registers a stdlib RFC-3339 check. Suite green with no residuals.

```text
source   2d2eaf9
CI       run 36768831367 — success 14/14
image    ghcr.io/imtarget05/maia-maia-api:2d2eaf9…@sha256:4398f981… (run 36768831564, provenance attested)
runtime  Azure revision predates streaming (deployed 47110b8 era); Render CD success
```

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
Feature implementation      VERIFIED (dd39026, CI-backed at 2d2eaf9)
Typed SSE contract          VERIFIED
Auth / tenant isolation     VERIFIED
Approval-safe execution     VERIFIED
Disconnect delivery stop    VERIFIED
Partial-write discard       VERIFIED
Mutation controls           VERIFIED
Mock TTFT evidence          VERIFIED (orchestration only)
Real provider token stream  PARTIAL / provider-dependent (fallback documented)
Compute cancellation        PARTIAL (delivery stops, in-flight sync call drops result)
Citations frame contract    VERIFIED (always emitted; empty when nothing retrieved)
Grounded delivery on the deployed revision   NOT VERIFIED (probe returned citations([]); no vector store reachable)
Full suite                  see the measured-figure block below
Canonical SHA               a82f24b (code, CI-backed) + docs commit below
```

### Full-suite count: which figure is current

The `13/13 @ a82f24b` and `998/12/2-deselected/3` figures below are a **dated CI
run record for SHA `a82f24b`** (run `36819947283`), retained because the
streaming evidence in §2–§4 was produced there. They are **not** the current
count. `.github/workflows/ci.yml` now defines **15** job keys.

The current measured figure for the canonical suite on `main` at `8ced0695`,
with `MAIA_EMBED_FORCE_HASH=1` and
`pytest tests/ -m "not live and not infra" --strict-markers`:

```text
1163 passed, 14 skipped, 2 deselected, 3 xfailed, 0 failed
```

The 14 skips are environment-gated, not failures: 10 need a running Qdrant
(`tests/test_tracing.py`, `tests/test_threshold_regression.py`,
`tests/test_golden_eval.py` — each skips with "Qdrant not available - set
QDRANT_URL"), 1 needs `azure-search-documents`, 2 need `MAIA_POSTGRES_DSN`, and
1 is a deliberate skip at `tests/test_azure_identity.py:564`. Docker was
unavailable during that run, so a Qdrant-present figure was **NOT** re-measured
and no "with services" number is extrapolated here.

## 9. Cloud closeout (Azure, exact canonical SHA)

```text
source    a82f24b2123b5abceeeb5264677aa81bbc437df7
CI        run 36819947283 — success 13/13 (historical; ci.yml now defines 15 job keys)
image     ghcr.io/imtarget05/maia-maia-api:a82f24b2123b5abceeeb5264677aa81bbc437df7@sha256:9be70ed14aaa849c17b8e9544dca284f853163895f039ed804e664f13c091514 (run 36819947364, provenance attested)
revision  ca-maia-api--0000006 — the revision the probes below were run against
```

**Two identities, and which one serves traffic is NOT VERIFIED.** Two docs name
different revisions as authoritative and nothing committed establishes which
receives traffic:

| identity | revision / image | what backs it |
|---|---|---|
| revision backing this streaming evidence | `ca-maia-api--0000006` / `a82f24b` | the probe results in this section |
| newest documented deployment | `ca-maia-api--0000012` / `b53aca4` | `docs/azure-integration.md` §7 |

The `100% traffic, Healthy` wording that used to sit here described the state at
probe time. It is retained as **what the probe observed then**, not as the
current deployment state — no retained artifact re-establishes it. Which
revision currently serves traffic is **NOT VERIFIED**.

Live probes against `--0000006` (fresh container FS, Qdrant unreachable):

- `/health` → `{"status":"ok","version":"0.4.0"}`
- unauthenticated `/chat/stream` → 401
- authenticated `/chat/stream` → `meta → 5×token (seq 1-5) → citations([]) → done` exactly once, `finish_reason: stop`, no traceback
- `/chat` parity → JSON `status: error` (`error:ConnectionError`), no 500

The error-path defect found by the first probe (`AttributeError` on
`llm.mode` when the stack is down) was fixed in `a82f24b` (degrade to
`"unknown"`, regression test `tests/test_agent_error_path.py`); the re-probe
above is against the fixed build. Grounded-answer delivery is not verifiable
in this environment (no vector store) — the probe returned `citations([])`.
Retrieval mechanics are covered by CI, not by this probe
(`tests/test_agent_task_benchmark.py::test_task001_retrieval_citation_no_tool`
retrieves and asserts a cited chunk offline). `2d2eaf9` and its digest are
historical.

## 8. Approved CV wording (do not strengthen)

> Implemented authenticated multi-tenant SSE response streaming with a
> typed `meta → token* → citations → done` contract, disconnect-aware delivery
> cancellation, partial-write discard, and approval-safe agent execution. The
> `citations` frame always ships and is **empty when retrieval yields nothing**;
> grounded retrieval itself is covered by the CI retrieval tests
> (`tests/test_agent_task_benchmark.py::test_task001_retrieval_citation_no_tool`),
> not by the live probe.

Token variant (only with the fallback clause):

> Implemented SSE streaming with provider token streaming when available and
> deterministic chunked fallback, preserving tenant isolation, approval-safe
> execution, and a citations frame that is populated from retrieval and empty
> when nothing is retrieved.

Why the earlier "grounded citations" phrasing was withdrawn: it asserted a
delivery property the only retained live probe contradicts. That probe recorded
`citations([])` (§9, "Live probes against `--0000006`") because no vector store
was reachable in that environment, so no grounded answer was ever observed
end-to-end against the deployed revision.

Frame contract, verified in `src/maia/api.py::chat_stream`: all three terminal
branches — `approval_required`, empty/abstain, and answered — read
`citations = result.get("citations") or []` and then emit exactly one
`citations` frame, so the frame's presence is not evidence of grounding.

Never claim: WebSocket, lower total latency, scale, compute cancellation, or
grounded citations from a live deployment.
