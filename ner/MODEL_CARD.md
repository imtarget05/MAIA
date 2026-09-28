# MODEL CARD — MAIA Vietnamese NER (PhoNER_COVID19)

## Model
- **Primary (this card):** BiLSTM tagger — word-embedding 128 + 1-layer BiLSTM 128
  + linear, greedy BIO decode. ~2M params, `ner/checkpoints-bilstm/model.pt` (3MB).
  Test micro-F1 **0.8325** (n=3000).
- **DistilBERT-multilingual (Colab T4, full data):** 3 epochs, batch 16,
  train_loss 0.1166 — `ner/checkpoints/` (final + checkpoint-315/630/945),
  `training_log.json` + Drive eval report `eval/ner-report-distilbert-2026-09-28.json`:
  test micro-F1 **0.9091** (n=3000, P 0.8996 / R 0.9188).
- **Independent verification (local CPU, same weights):** n=300 → 0.8505;
  n=1000 → **0.8694** (`eval/ner-report-distilbert-verify-1000-2026-09-28.json`).
  Scores rise with n (test set ordered by difficulty); the 0.9091 full-set claim
  is consistent with this trend. Note: the Drive report's `macro_f1` equals its
  `micro_f1` (eval-script version difference) — local runs report true macro
  (0.8258 @ n=1000).

## Data
PhoNER_COVID19 (Nguyen et al., 2020), word-level Vietnamese, 10 entity types
(PATIENT_ID, PERSON/NAME, AGE, GENDER, OCCUPATION/JOB, LOCATION, ORGANIZATION,
SYMPTOM_AND_DISEASE, TRANSPORTATION, DATE). Splits: 5027 / 2000 / 3000 sents.
Local: `ner/data/` (gitignored, download from
https://github.com/VinAIResearch/PhoNER_COVID19).

## Training (reproducible)
`./ner/.venv/bin/python ner/bilstm.py --epochs 8` — Adam 1e-3, batch 32,
max_len 64, seed 42, CPU-only Intel Mac, ~15 min. Log: `ner/bilstm.log`,
`checkpoints-bilstm/log.json`. Curve: dev F1 0.50 → 0.65 → 0.72 → 0.74 →
0.76 → 0.76 → 0.79 → 0.77 (no overfit cliff; epoch 7 best).

## Results (test, n=3000 — `eval/ner-report-2026-09-28.json`)
| metric | value |
|---|---|
| micro-F1 | **0.8325** |
| macro-F1 | 0.7739 |
| precision / recall (micro) | 0.8568 / 0.8096 |
| paper SOTA PhoBERT-large | 0.945 |
| paper mBERT baseline | ~0.90 |

Per-type F1: PATIENT_ID 0.955, AGE 0.926, GENDER 0.919, DATE 0.912,
LOCATION 0.806, TRANSPORTATION 0.775, SYMPTOM 0.722, ORGANIZATION 0.684,
NAME 0.645, JOB 0.395.

## Error analysis (honest)
- JOB worst (0.395): occupation nouns ("bệnh_nhân", "bác_sĩ") overlap common
  nouns; word-only embeddings lack context of a transformer. This is exactly
  the gap a BERT fine-tune closes (paper: JOB 0.88+ with PhoBERT).
- NAME/ORGANIZATION mid (0.65/0.68): greedy decode splits multi-token spans;
  a CRF layer (Lample et al. full architecture) is the documented next step.

## Limitations / bias
COVID-domain Vietnamese news/social text (2020). Do NOT use for medical
decisions. PII-like entities (PATIENT_ID/NAME) — inference outputs must pass
the same PII redaction as the rest of MAIA (`guardrails/pii`).

## Use in MAIA
`ner/ie_retrieval_demo.py` — entities from a query become MUST-match filters
on retrieval candidates (`eval/ie-retrieval-demo-<date>.json`). Wiring into
`TOOL_REGISTRY.extract_entities` is tracked work (needs transformers runtime
in the API image).
