# MAIA — Interview Evidence Kit

> MAIA: internal RAG knowledge platform + LangGraph HITL agent.
> Suite figure (measured on `main` at `658db396`): **1167 passed, 14 skipped,
> 2 deselected, 3 xfailed, 0 failed** — see the README Testing section. There is
> **no** "128 tests passing" badge in `README.md`; an earlier draft of this file
> cited one and it never existed.
> Platform: **Azure Container Apps is current**; Render is superseded legacy.
> Phase-2 addition: adversarial/HITL-boundary coverage in
> `tests/test_injection_adversarial.py` (incl.
> `test_adversarial_prompt_cannot_bypass_hitl_offline`) and
> `tests/test_agent_task_benchmark.py::test_task002_high_risk_requires_approval`.

---

## 1. STAR story

**Situation.** Internal HR/IT/Security questions were answered from memory or
scattered docs — hallucination risk, no citations, and autonomous actions
(leave requests, IT tickets) had no approval boundary.

**Task.** Build a grounded Q&A + action agent: hybrid retrieval, evidence gate
with honest refusal, per-answer citations, and HITL approval for every
side-effect — offline-first for dev, multi-tenant by `tenant_id`.

**Action.**
- Hybrid retrieval: Qdrant dense top-10 + BM25 sparse top-10 fused by RRF
  (k=60) → evidence gate (similarity ≥ 0.3) → cross-encoder rerank → citation
  assembly (`[S1]`, `[S2]` with file/section/snippet).
- HITL: LangGraph StateGraph + SQLite checkpointing; side-effects interrupt and
  resume only via `/actions/confirm`.
- Safety: 2-layer PII scan (ingest + output guardrail), role-allowlist emails,
  tenant isolation at Qdrant payload + DB, `MAIA_EMBED_FORCE_HASH=1`
  deterministic embedder for tests.

**Result.** Suite green at the measured figure above (1163 passed, 0 failed);
honest-refusal path covered by tests (no-evidence → no-answer); HITL boundary
covered by the adversarial and benchmark tests above (untrusted prompt cannot
override approval). Deployed on Azure Container Apps — but the deployed-revision
and end-to-end-cloud claims are split by state in the README, and an
authenticated Azure SSE probe plus the Qdrant Cloud path are **NOT VERIFIED** (no
retained artifact). Do not upgrade that to "live and verified" in an interview.

### The answerability gate does not currently pass

Say this before it is asked. Gate 8B-C reports `status: "FAIL"` and exits 1;
9 of 11 checks pass, `B8B1` and `B8B3` fail. Measured abstention is
**1 of 9** labelled no-answer queries (rate 0.1111, **n = 9**), the classes are
**not separable** by similarity (max no-answer `top_dense` **0.6957** vs min
answerable **0.3140** — they overlap), and the no-answer corpus is labelled
**7 usable of 9**.

This was not tuned away: no threshold was changed and no golden row was
relabelled to reach a target. Closing it needs a different decision signal
(answer-span verification or NLI entailment), not a threshold change. Numbers:
`eval/README.md:16-40` and the artifact
`../docs/evidence/e2e/gate8b-abstention.json`, which lives in the sibling
`Projects/docs` repo and is therefore **absent from a MAIA-only clone**.

## 2. System-design Q&A

**Q1: Why RRF over dense-only or sparse-only retrieval?**
Dense misses exact policy keywords; sparse misses paraphrase. RRF fuses both
rankings without score calibration, lifting recall on mixed query styles.
Cost: two retrievals per query + rerank latency — bounded by top-10 cutoffs and
justified because a wrong HR answer costs more than 200 ms.

**Q2: Why refuse instead of answering with low confidence?**
For HR/Security policy, a fluent wrong answer is worse than no answer — it
creates liability. The ≥0.3 gate is the mechanism, and honest refusal is the
intended behaviour. But the intent is not yet the measured result: the gate
currently **authorises 8 of 9** labelled no-answer queries and the classes are
not separable by similarity at all (numbers above). So the honest trade-off
statement today is: the refusal *policy* is right, the refusal *implementation*
is measurably not delivering it yet, and the fix is a better decision signal
rather than a stricter threshold. Trade-off that is expected to remain: a higher
non-answer rate on thin corpora, which is the honest signal to grow the corpus.

**Q3: Why LangGraph interrupts instead of a simple approve-button callback?**
Side-effects need durable pause/resume: server restarts must not lose or double
execute the action. SQLite-checkpointed interrupts give exactly-once resume
semantics via thread id. A stateless callback would re-execute on retry.

**Q4: What is the SPOF / scaling limit?**
Single Qdrant + SQLite checkpoint store per deployment; tenant isolation is
logical (payload filter), not physical. Multi-region or per-tenant hard
isolation would need Qdrant collections per tenant + Postgres checkpointer —
deferred until tenant count justifies it.

## 3. Live-demo script (5 steps)

Route names below are the real ones in `src/maia/api.py`. An earlier draft of
this section used `/ask` and `/actions`; **neither route exists** — there is no
`POST /ask` and no bare `POST /actions`. Q&A goes through `POST /chat` (or
`POST /query` for the pipeline form) and the side-effect path goes through
`POST /agent/chat`.

```bash
# 1. Boot API + UI (offline-first: no Cloudflare creds -> local mocks)
docker compose up --build
# 2. Ask a policy question (citations projected from retrieved chunks)
curl -s http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"question":"What is the leave approval policy?","session_id":"demo"}'
# 3. Ask something outside the corpus -> refusal path
curl -s http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"question":"What is the cafeteria menu on Mars?","session_id":"demo"}'
# 4. Request a side-effect action -> interrupt, pending approval
curl -s http://localhost:8000/agent/chat -H 'Content-Type: application/json' \
  -d '{"question":"Đăng ký IT ticket: VPN hỏng","session_id":"demo"}'
# 5. Approve explicitly -> resumes and executes once
curl -s http://localhost:8000/actions/confirm -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","approved":true}'
```

The two things that were wrong: the **routes** (`/ask`, `/actions` do not
exist) and the **confirm field** — `ConfirmReq` takes `session_id`, not
`thread_id`. The `question` field name was correct and is kept.

| Step | Route (`src/maia/api.py`) | Expected |
|---|---|---|
| 2 | `POST /chat` | answer with `[S1]` citations projected from retrieved chunks. Note: the citation frame is **empty when retrieval returns nothing** — do not present a non-empty citation list as proof of grounding |
| 3 | `POST /chat` | refusal/empty-answer path, no fabricated answer. Officially this path is the one Gate 8B-C measures as **failing** (1/9), so demo it as implemented behaviour, not as a passing gate |
| 4 | `POST /agent/chat` | `needs_approval` + `pending_action` + `session_id`, nothing executed |
| 5 | `POST /actions/confirm` | executes once; reject path aborts with audit trace. Pending state is readable at `GET /actions/pending/{session_id}` |

Auth note: `/chat`, `/agent/chat`, `/actions/*` are behind
`get_current_active_user` (`POST /auth/login` first). `GET /health`,
`GET /ready` and `GET /metrics` are not. `tenant_id` is accepted on `ChatReq`
but the stream path derives the tenant from the authenticated user, not the body.
