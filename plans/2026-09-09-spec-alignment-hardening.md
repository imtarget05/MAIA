# Focus Plan: Spec-Alignment Hardening (Option 1)

> **Created**: 2026-09-09 | **Status**: complete | **Owner**: build agent
> **Goal**: close the 3 open questions in `docs/spec.md` with evidence, and lock a
> `contact_usability_rate` regression gate into CI.

## State when planned

- Simplification Steps 1–4 complete: flat-only `config.py`, `_archive/` isolation,
  JSON-only BM25 cache, canonical `_ingest_rawdocs` tail. Offline: 215 passed / 2 skipped.
- No active plan/contract in `.ai/harness/handoff/`; `tasks/current.md` holds the
  simplification snapshot; `tasks/todos.md` holds deferred goals (guardrails merge, stream/CRAG/LTM).
- Eval reality: `query()`-based eval needs live Qdrant (none locally); offline eval
  possible via `InMemoryVectorStore` harness (test pattern).

## Task Breakdown

1. **EN recall@k=0.0 root cause** — head-to-head experiment (hash vs FastEmbed,
   same enterprise corpus + `HybridRetriever`, `InMemoryVectorStore`).
   - [x] Hash mode: en hit@k 0.6 / recall@k 0.55 @ fused top-8
   - [x] FastEmbed mode: en hit@k 1.0 / recall@k 1.0
   - [x] Gate probe: hash top-1 dense 0.14–0.27 < 0.3 threshold → refusal → 0.0 end-to-end
   - [x] Write `docs/researches/en-recall-investigation.md`
2. **CI gate for `contact_usability_rate`** — `--fail-under-contact 0.8` flag on
   `maia.eval` + step in the `retrieval-regression` CI job (has Qdrant + ingested docs).
   - [x] `eval.py`: factored `contact_gate(report, threshold)` + CLI flag (nonzero exit on breach)
   - [x] Unit test for gate logic (offline, no Qdrant)
   - [x] `.github/workflows/ci.yml`: gate step after retrieval regression test
3. **Resolve spec open questions** — patch `docs/spec.md`:
   - Q1 (en 0.0): hash-mode artifact + evidence-gate interaction, NOT missing EN data. Answered.
   - Q2 (CI gate): yes — ≥0.8 on `contact_usability` split in `retrieval-regression` job.
   - Q3 (Kafka/CRAG scope): v1 out of scope (matches `_archive/` + disabled-by-default flags).
4. **Verify + record** — offline pytest, gate dry-run (logic level), update `tasks/current.md`.

## Verification

- `MAIA_EMBED_FORCE_HASH=1 pytest tests/ -q` (minus Qdrant-dependent): all green.
- New gate unit test passes; `maia.eval --help` shows the flag.
- `rg pickle src/` → comment only; no new deps; no archived-module refs.

## Acceptance criteria

- `docs/researches/en-recall-investigation.md` exists with numbers + causal chain.
- CI fails a build when `contact_usability_rate < 0.8` on the `contact_usability` split.
- `docs/spec.md` has zero open questions; v1 scope explicitly excludes Kafka/CRAG.
- `tasks/current.md` reflects the completed hardening slice.
