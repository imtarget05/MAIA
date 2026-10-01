# Evaluation (canonical)

**Canonical dataset: `eval/golden/` (10 groups, 98 rows total).**
Legacy top-level `eval/dataset.jsonl` (6 rows), `eval/retrieval_dataset.jsonl`
(4 rows) are kept for history only — do NOT use them to claim quality.

| group | rows | covers |
|---|---|---|
| vi_policy / en_policy | 15 / 10 | policy QA, citations |
| paraphrase / exact | 15 / 8 | retrieval robustness |
| no_answer / ambiguous | 9 / 6 | honest refusal |
| injection / unauthorized | 6 / 6 | prompt-injection, access control |
| tool_request | 8 | agent tool routing |
| contact_usability | 15 | PII-redaction contact survival |

## MEASURED DEFECT — the abstention gate does not pass, and cannot be made to

**Status: MEASURED DEFECT / PARTIAL.** Gate 8B-C reports `status: "FAIL"` and
exits non-zero. That is the finding. Nothing below has been tuned to change it.

**Measured abstention.** `1 of 9` labelled no-answer queries were refused by the
evidence gate — **abstention rate 0.1111, n = 9**. The denominator is stated
because the rate alone is not a result: with `n = 9`, one row moves it by 0.1111.

**Usable sample.** After corpus review (below), `no_answer.jsonl` yields
**7 usable rows of 9**: `9 = 7 usable + 1 pending (NOANS-008) + 1 rejected
(NOANS-009) + 0 schema conflicts`. The eval audit reports `usable=7`.

**The two classes are NOT separable by similarity.**

| | measured |
|---|---|
| max no-answer `top_dense` | **0.6957** |
| min answerable `top_dense` | **0.3140** |

`max(no-answer) >= min(answerable)`, so the score distributions **overlap**. The
consequence is exact and is the reason no threshold change is proposed: a single
scalar threshold separates two classes only when every no-answer score sits below
every answerable score. Any threshold low enough to refuse all 9 no-answer
queries also refuses the weakest genuine answer. **No value of
`SIMILARITY_THRESHOLD` separates these classes.** A cosine score on the top chunk
measures *topical similarity*, not *answerability* — no-answer questions about
retirement or stock options are topically adjacent to `HR_Policy` and
`Benefits`, so they retrieve a confident-looking chunk that does not contain the
answer. `docs/eval/proto_answerability_signals.py:7-8` reached this conclusion
before the gate was ever run.

**Closing this needs a different decision signal**, not threshold tuning:
answer-span verification (does the retrieved chunk contain a value *of the type
the question asks for* — a number, a date, an id) or an NLI entailment check
(does this passage support this claim).

### What was NOT done to reach this verdict

- **No threshold was tuned to reach a target.** The gate's only YAML threshold is
  `ABSTENTION.min_answerable_authorized`, read unmodified from
  `gate8b_thresholds.yaml`; the similarity threshold is the configured
  `SIMILARITY_THRESHOLD` (`src/maia/config.py`), also unmodified. No
  abstention-rate target was chosen and then satisfied.
- **The corpus was labelled by review, not adjusted to fit a metric.** Every
  no-answer row now carries a `review_status` and a `verification` block naming
  what was searched in `data/enterprise/`. Where the corpus contradicted a label
  the row was **rejected**, not deleted and not flipped to make a number look
  better. `NOANS-009` is the worked example and is retained in place.
- **Proving the dataset edit did not move the gate**: re-running the gate after
  the triage changed exactly one field of the emitted artifact —
  `integrity.no_answer_sha256`. Every metric, including `status`,
  `abstention_rate`, `separable`, `max_noanswer_top_dense` and
  `min_answerable_top_dense`, is byte-identical. The gate does not read
  `review_status`.

### Row-level review of `no_answer.jsonl`

| id | review_status | why |
|---|---|---|
| NOANS-001 | VERIFIED | no lending/mortgage/interest-rate content in any of the 8 corpus docs |
| NOANS-002 | VERIFIED | director titles exist but no salary figure is attached to any of them; original note's "lương only as a bonus unit" was **imprecise** and is corrected in the row |
| NOANS-003 | VERIFIED | `reason=OUT_OF_SCOPE`; corpus is entirely HR/IT/expense policy, no culinary content |
| NOANS-004 | VERIFIED | `Leave_Policy.md` enumerates the full leave catalogue; no retirement or pension clause |
| NOANS-005 | VERIFIED | `Benefits.md` §5 awards table is cash-and-time only; no equity instrument |
| NOANS-006 | VERIFIED | no workplace pet policy. Needed a **matcher** fix, not a data fix: substring matching saw `cat` inside `Authentication` (`VPN_Guide.md` §4) and reported a correct row as mislabelled |
| NOANS-007 | VERIFIED | adjacent-topic case: `Expense_Policy.md` §3 covers business-travel transport, which grants no parking entitlement |
| NOANS-008 | **PENDING** | deliberately unresolved. `Leave_Policy.md` states `Hiệu lực 01/01/2024` in its header, so the corpus *does* contain the fact. Whether header metadata counts as an answerable span is an open policy question for an owner — the row is left out of every metric rather than pushed into one |
| NOANS-009 | **REJECTED** | corpus contradicted the label: `Leave_Policy.md` states the carry-over cap is 3 days |

### Two oracle fields, two matchers

`probe_terms` is the legacy input the gate reads (B8A2). `absence_probe_terms`
is the field review asserted absent, and is what
`eval/verify_eval_rows.py` requires before a `NO_ANSWER` row can be verified.
Both hold the same terms on every reviewed row; the audit checks their **union**,
so adding the reviewed field can never reduce coverage.

The absence matcher is **token-sequence**, not substring, and shares
`maia.textnorm.norm_tokens` with the production retriever. This was a real fix,
not cosmetics: under the old substring matcher `NOANS-006` was reported as
`absence_probe_term 'cat' IS PRESENT in ['VPN_Guide.md'] -> this row is
mislabelled`, where the only match was the word **Authentication**. The tempting
"fix" — deleting the probe term `cat` — would have turned a correct row's check
green by weakening the question. The matcher was repaired instead.

Run:

```bash
PYTHONPATH=src python -m maia.eval --all --top-k 3 --manifest eval/reports/
PYTHONPATH=src python -m maia.eval --all --fail-under-hit 0.5 --fail-under-faith 0.4
```

Metrics per group: `hit@k`, `recall@k`, `mrr`, `context_precision`,
`faithfulness_proxy` (keyword overlap, legacy), **`faithfulness_embed`**
(mean max-cosine answer-sentence → retrieved chunk, hash embedder offline),
`relevance_proxy`, `false_refusal_rate`, `refusal_accuracy`, `leakage_rate`.
`faithfulness_embed=None` when a group has no retrieved context (honest no-signal,
not 0.0).
