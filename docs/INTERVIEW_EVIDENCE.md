# MAIA — Interview Evidence Kit

> MAIA: enterprise RAG knowledge platform + LangGraph HITL agent.
> README badge: 128 tests passing. Live: API + UI on Render (see README).
> Phase-2 addition: 5 adversarial/HITL-boundary tests (see root HARD_TEST_REPORT.md).

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

**Result.** 128-test suite green; honest-refusal path covered (no-evidence →
no-answer); HITL boundary covered by 5 Phase-2 adversarial tests (untrusted
prompt cannot override approval). Live API + UI deployed on Render.

## 2. System-design Q&A

**Q1: Why RRF over dense-only or sparse-only retrieval?**
Dense misses exact policy keywords; sparse misses paraphrase. RRF fuses both
rankings without score calibration, lifting recall on mixed query styles.
Cost: two retrievals per query + rerank latency — bounded by top-10 cutoffs and
justified because a wrong HR answer costs more than 200 ms.

**Q2: Why refuse instead of answering with low confidence?**
For HR/Security policy, a fluent wrong answer is worse than no answer — it
creates liability. The ≥0.3 gate converts uncertainty into an explicit refusal
the user can act on. Trade-off: higher non-answer rate on thin corpora, which
is the honest signal to grow the corpus.

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

```bash
# 1. Boot API + UI (offline-first: no Cloudflare creds -> local mocks)
docker compose up --build
# 2. Ask a policy question (grounded, cited)
curl -s http://localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","question":"What is the leave approval policy?"}'
# 3. Ask something outside the corpus -> honest refusal (no hallucination)
curl -s http://localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","question":"What is the cafeteria menu on Mars?"}'
# 4. Request a side-effect action -> interrupt, pending approval
curl -s http://localhost:8000/actions -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","action":"create_it_ticket","params":{"title":"VPN broken"}}'
# 5. Approve explicitly -> resumes and executes once
curl -s http://localhost:8000/actions/confirm -H 'Content-Type: application/json' \
  -d '{"thread_id":"<id-from-step-4>","approved":true}'
```

| Step | URL | Expected |
|---|---|---|
| 2 | `POST /ask` | Answer with `[S1]` citations to source file/section |
| 3 | `POST /ask` | Refusal (evidence gate < 0.3), no fabricated answer |
| 4 | `POST /actions` | `pending_approval` + thread id, nothing executed |
| 5 | `POST /actions/confirm` | Executes once; reject path aborts with audit trace |
