# Evaluation (canonical)

**Canonical dataset: `eval/golden/` (10 groups, 95 rows total).**
Legacy top-level `eval/dataset.jsonl` (6 rows), `eval/retrieval_dataset.jsonl`
(4 rows) are kept for history only — do NOT use them to claim quality.

| group | rows | covers |
|---|---|---|
| vi_policy / en_policy | 15 / 10 | policy QA, citations |
| paraphrase / exact | 15 / 8 | retrieval robustness |
| no_answer / ambiguous | 6 / 6 | honest refusal |
| injection / unauthorized | 6 / 6 | prompt-injection, access control |
| tool_request | 8 | agent tool routing |
| contact_usability | 15 | PII-redaction contact survival |

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
