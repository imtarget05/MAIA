# WAVE 0 — BASELINE REPORT: capability vs runtime

```text
measured_at : 2026-10-02
question    : what does the shipped image EXECUTE, not what does the source contain?
method      : MAIA's own code, run against requirements.api.txt in a clean venv
             (the exact file Dockerfile.api installs) + the real abstention gate
raw logs    : 2026-10-02-baseline.log, ../terraform-phase1/2026-10-02-gate.log
```

## 1. The six priorities, measured

| # | Claim under test | Source says | Runtime says | Verdict |
|---|---|---|---|---|
| 1 | BM25 in the production image | `rank-bm25>=0.2.2` in `requirements.txt`; `HybridRetriever` documents dense+BM25→RRF | **ABSENT** from `requirements.api.txt`. `retriever.py:87-93` swallows the ImportError, `self._bm25 = None`; `retrieve():119` then gates the sparse leg on that, so **RRF receives one ranking list** | **NOT RUNNING** |
| 2 | Reranker in the production image | `cross-encoder/ms-marco-MiniLM-L-6-v2` via `reranker.py` | **ABSENT**. `mode == "fallback"`; measured `input order == output order == [c1,c2,c3]` — a **no-op sort** returning the RRF order unchanged | **NOT RUNNING** |
| 3 | True SSE token streaming | AI-PRODUCTION-GATE requires `LLM emits delta → SSE token event immediately` | `api.py:1249` takes a finished `result["answer"]` string, `:1264` iterates `split_tokens(answer)`. A real `chat_stream` exists on both LLM classes (`llm.py:290`, `:432`) but the endpoint never calls it | **REPLAY, NOT STREAMING** |
| 4 | Redis distributed rate limit | `config.py:255` documents `REDIS_URL` for multi-replica | **ABSENT** from the image and `REDIS_URL` unset ⇒ `api.py` uses the process-local `_rl_hits` dict; every replica keeps its own counter | **PER-PROCESS** |
| 5 | PostgreSQL durable state | `persistence.py` implements `AsyncPostgresSaver` | **ABSENT** (`psycopg` not in the image, `MAIA_POSTGRES_DSN` unset). The API graph is built on `SqliteSaver` (`langgraph_agent.py:576`); streaming uses `MemorySaver` (`:542`) | **SQLite, ONE REPLICA** |
| 6 | Abstention / evidence gate | README reports the gate "exits 1" | Re-measured with real embeddings: **1/9 abstained (rate 0.111)**; 8 labelled no-answer queries were AUTHORISED | **FAILING, ROOT CAUSE MEASURED** |

## 2. Why items 1–5 are dormant — and what each fix actually costs

`requirements.api.txt` is not an oversight. Its header gives the reason: ACA
Consumption is 1 GiB (`infra/modules/apps/main.bicep:51`, "must not be reduced:
the retrieval stack plus the in-process auth database OOM below it"), and
`torch` + `sentence-transformers` would not fit. That reasoning is sound **for
torch and wrong for BM25**:


| Capability | Cost in the image | 1 GiB verdict |
|---|---|---|
| `rank-bm25` | **8.6 KB wheel, pure Python, zero dependencies** (measured by download) | **Fits trivially.** Its exclusion is not a memory decision; it reads as an omission inherited from the same list that dropped `fastembed` |
| `redis` | small client | Fits, but needs a **managed Redis** to be worth anything |
| `psycopg` | small client | Fits, but needs a **managed PostgreSQL** |
| `sentence-transformers` + `torch` | ~2 GB of wheels, ~90 MB of weights | **Does not fit.** Genuinely blocked on sizing |

So the honest split is:

- **BM25 is a one-line fix with no architectural argument against it.** Until it
  is added, "hybrid retrieval (dense + BM25, RRF)" is a source-only claim.
- **The reranker is a real architecture decision** needing a choice, not a
  dependency line: Dedicated ACA profile, a separate rerank service, or keeping
  the fallback and calling it *fallback* everywhere.
- **Redis and PostgreSQL are Wave 2/7 infrastructure work** (managed instances
  + Terraform modules), not packaging.

## 3. Item 6 in detail — the abstention defect, measured

`gate8b_abstention.py` re-run with real `fastembed` embeddings
(`embedder mode=fastembed`, 45 chunks, threshold 0.3):

```text
9/11 checks PASS — GATE 8B-C FAIL
failing: B8B1, B8B3
abstention: 1/9 refused (rate=0.1111, n=9)
separable_by_similarity: False (max no-answer 0.6957 vs min answerable 0.3139)
```

What this rules **in**:

- the dataset is correct — B8A passed; the oracle verified the 7
  `NO_SUPPORTING_DOCUMENT` rows have no supporting concept in the corpus;
- the production path is what was measured — B8B2b drove the real
  `maia.pipeline_query.query()` and agreed with the mirror on 9/9 rows;
- the gate is not a no-op — B8C1 lowered the threshold to 0.01 and 9/9 became
  authorised, so the control can fail;
- tenant isolation holds here — B8B5: tenant A querying tenant B's
  compensation retrieved **0** tenant-B chunks;
- the classes **overlap**: max no-answer `top_dense` 0.6957 > min answerable
  0.3139.

## 4. Consequences for the FINAL FREEZE checklist

Several items on that checklist are **not** merely unverified — they are
currently **false if stated without qualification**:

- `real BM25` · `real hybrid` · `real RRF` — the RRF code runs, on one list.
- `reranker in runtime` — the code runs, as a no-op sort.
- `true SSE` — the endpoint streams, replaying a completed answer.
- `2+ replica correctness` — with per-process rate limiting and SQLite
  checkpoints, cross-replica HITL cannot hold today.
- `no-answer / abstention correct` — measured failing.

None of these is fixed by a commit that adds one line to a requirements file.
The BM25 fix is genuinely small; the reranker, the durable state and the
abstention signal are not, and each is called out above with what it would take.

## 5. What the durable control should be

Re-running this measurement is a diagnostic, not a gate: it needs a clean venv
and only changes when `requirements.api.txt` changes. The control that stops
regression is a **dependency contract test** asserting that every capability the
product claims is either importable from `requirements.api.txt` or explicitly
declared dormant in a checked-in list, so a capability cannot silently vanish
from the image again. That is the first item of the next wave.


That last point is the finding. **No setting of `SIMILARITY_THRESHOLD` can
separate them** — 8 no-answer questions score above the weakest genuine answer.
Cosine similarity on the top chunk measures *topical* similarity, not
*answerability*: a question about retirement or stock options is topically close
to HR_Policy and Benefits, so it retrieves a confident-looking chunk that does
not contain the answer. Tuning the threshold until the gate passes would also
refuse correct answers, which is exactly why B8B2 exists as a required control.

Closing it needs a different decision signal — answer-span verification (does the
retrieved chunk contain a value of the type the question asks for) or an NLI
entailment check. `docs/eval/proto_answerability_signals.py` reached the same
conclusion before this gate was run.
