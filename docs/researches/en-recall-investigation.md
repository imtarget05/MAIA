# EN `recall@k=0.0` — root cause: hash-mode artifact, not missing EN data

> **Date**: 2026-09-09 | **Slice**: Spec-Alignment Hardening (spec Q1)
> **Question**: README eval table shows `en_policy recall@k=0.0` — hash-mode artifact or missing EN data?

## Answer

**Hash-mode artifact amplified by the evidence gate.** The enterprise corpus fully
covers the EN questions (FastEmbed reaches `hit@k=1.0/recall@k=1.0` on the same
corpus + retriever). Nothing is missing; the offline embedder just can't see it,
and the evidence gate turns weak scores into a hard refusal.

## Experiment

Harness (`/tmp/en_recall_exp.py`, throwaway): `data/enterprise` → `split_documents`
(512/50) → `Embedder` (hash vs FastEmbed) → `InMemoryVectorStore` →
`HybridRetriever` (10/10/8, RRF k=60) → per-split `hit@k`/`recall@k` @ fused top-8.
Same code path as `tests/` (no Qdrant needed). Full `query()` eval was impossible
locally (no Qdrant); see "gate interaction" for the pipeline-level link.

| Split (n) | Hash: hit@k / recall@k | FastEmbed: hit@k / recall@k |
|---|---|---|
| `en_policy` (10) | 0.600 / 0.550 | **1.000 / 1.000** |
| `vi_policy` (15, control) | 0.933 / 0.867 | 1.000 / 0.967 |
| `contact_usability` (8) | 0.750 / 0.688 | 1.000 / 0.938 |

Embedder modes confirmed: `hash` (`MAIA_EMBED_FORCE_HASH=1`) vs `fastembed`
(`paraphrase-multilingual-MiniLM-L12-v2`, dim 384).

## Causal chain (3 links)

1. **Hash embeddings carry no semantics.** Quasi-orthogonal vectors → cosine ≈ 0
   for any non-identical pair. Cross-lingual EN-query → VI-corpus matching is
   impossible; only BM25 loanword hits (`VPN`, `laptop`, `ticket`, `email`) land —
   hence 0.55, not 0.0, at fused top-8.
2. **Evidence gate converts weak scores to refusal.** `pipeline_query.py:58-59`:
   `has_evidence = top_dense >= SIMILARITY_THRESHOLD (0.3)`. Probe (hash mode):
   top-1 dense = 0.143 ("annual leave days") and 0.272 ("VPN access" — the *correct*
   gold chunk `63a16140e921_0`). Both < 0.3 → `insufficient_evidence`, `citations=[]`.
3. **Eval measures citations.** `_eval_row` scores `hit/recall` off `citations[]`,
   so gate refusal reads as `recall@k=0.0` end-to-end (plus `TOP_K_FINAL=3` cutoff
   and score-fallback rerank on top).

## Conclusions

- **Not missing EN data**: FastEmbed 1.0/1.0 proves the gold chunks exist and the
  multilingual model bridges EN queries → VI corpus. No dataset fix needed.
- **Hash-mode eval numbers are a lower bound only** (README already labels them so);
  cross-mode comparison (hash vs FastEmbed) is invalid for absolute quality claims.
- **Gate behavior in hash mode is correct-by-design**: scores are meaningless, so
  refusal is the honest answer. Do not lower the threshold to "fix" hash-mode eval.
- Follow-up (not this slice): re-run full `query()` eval with FastEmbed + live
  Qdrant to confirm end-to-end EN `recall@k≈1.0`; needs Qdrant service (CI has it).

## Follow-up: FastEmbed end-to-end with live Qdrant (2026-09-09)

Local Qdrant (`docker-compose up -d qdrant`) + `maia.cli ingest --enterprise`
(8 docs, 25 chunks, `embed_mode=fastembed`) + `maia.eval --group en_policy --top-k 3`:

| Split | End-to-end (top-3, mock→real LLM) | Retrieval-only (fused-8, offline probe) |
|---|---|---|
| `en_policy` | hit@k **0.8** / recall@k **0.75** / ctx_prec 0.6 | 1.0 / 1.0 |
| `contact_usability` | hit@k 0.375 / recall@k 0.312, `contact_context_rate` **0.5**, `contact_usability_rate` 0.5 | recall 0.938 |

Findings:

- **New bottleneck is top-3 cutoff + fallback reranker, not embeddings.** Example —
  "Who do I contact about employee benefits?": fused top-8 contains the Benefits.md
  chunks (dense 0.3–0.5, healthy), but score-fallback rerank just replays fused order
  and the top-3 keeps HR_Policy chunks; gold contact chunk falls out. Matches the
  known audit gap (no cross-encoder, `rerank mode: fallback`).
- **Redaction layer is clean.** In all 8 contact rows `contact_usable ==
  contact_in_ctx`: wherever the gold chunk was retrieved, the role email survived
  (4/4); the 4 zeros are retrieval misses (recall 0), not redact failures.
  (This run used the real Cloudflare LLM, so the answer-level metric is genuine.)
- **Gate mechanics verified live**: `--fail-under-contact 0.8` exits 1 at 0.5.
  But locking 0.8 on the *unconditional* rate would red-light CI on retrieval depth,
  not redaction. **Resolution (Option C, 2026-09-09)**: `contact_in_ctx` redefined
  as conditional — `None` on retrieval miss (`hit==0`), so `contact_context_rate`
  measures pure redact survival among retrieved rows (4/4 = 1.0 on this corpus).
  CI gate locked at `--fail-under-contact 1.0`. Retrieval-depth improvement
  (cross-encoder rerank / top-k tuning) is a separate slice and lifts the
  unconditional `contact_usability_rate` independently.

## Repro

```bash
# hash mode
MAIA_EMBED_FORCE_HASH=1 PYTHONPATH=src python3 /tmp/en_recall_exp.py
# real embeddings
PYTHONPATH=src python3 /tmp/en_recall_exp.py
```
