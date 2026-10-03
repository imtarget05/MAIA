# ADR 0006: Separation of Retrieval Topical Similarity from Evidence Answerability

## Status
**ACCEPTED** (Documented Architectural Limitation & Two-Stage Design Path)

## Context & Problem Statement

In enterprise RAG systems, hallucination mitigation requires the system to reliably refuse questions that cannot be answered from the document corpus (honest abstention).

During benchmark evaluation of MAIA against the canonical golden dataset (`eval/golden/no_answer.jsonl`, 98 rows across 10 groups), Gate 8B measured the following distribution for top-ranked retrieved chunks:

| Metric | Measured Value |
|---|---|
| `max(no-answer)` top dense score | **0.6957** |
| `min(answerable)` top dense score | **0.3140** |
| Class separability | **FALSE** (distributions overlap) |
| Initial scalar abstention rate | **0.1111** (1 of 9 unanswerable queries refused) |

Because `max(no-answer) >= min(answerable)`, the score distributions overlap significantly. Questions regarding topics absent from the corpus (e.g., retirement pension, cryptocurrency bonuses, executive stock options) are topically adjacent to `HR_Policy.md` and `Benefits.md`. Dense embedding vectors project them into nearby semantic space, producing high cosine similarity scores ($\approx 0.70$) even though the text contains no factual answer to the question.

### The Scalar Threshold Fallacy

A single scalar similarity threshold ($\tau$) can only separate two classes if $\forall x \in \text{no-answer}, \forall y \in \text{answerable}: \text{score}(x) < \tau \le \text{score}(y)$. 

Since this condition is violated:
- Any threshold high enough to reject all no-answer queries ($\tau > 0.6957$) would simultaneously reject valid answerable queries with lower confidence ($0.3140 \le \text{score} \le 0.6957$).
- Any threshold low enough to accept all genuine answers ($\tau \le 0.3140$) accepts 100% of out-of-corpus queries.

### Signals actually measured, not just proposed

The next-phase design above was a hypothesis. It was then measured against the
labelled corpus (`docs/eval/proto_answerability_signals.py --signals`,
2026-10-03) so the decision rests on data rather than intuition. Dataset:
**9 no-answer rows, 85 answerable rows, 45 corpus chunks**.

| Signal | no-answer mean | answerable mean | separable? |
|---|---|---|---|
| A topical overlap | 0.353 | 0.483 | **no** |
| B type-supported (does the chunk carry a value of the asked type?) | 1.000 | 0.882 | **no** |
| C subject coverage | 0.688 (max 1.000) | 0.739 (min 0.000) | **no** |
| D subject + answer-type co-location in one sentence | 0.100 (max 0.500) | 0.347 (min 0.000) | **no** |

Every cheap lexical signal overlaps. Signal D is the most promising — it drops
no-answer from 0.688 to 0.100 — but it still fails:

- At the best split `t=0.375`: no-answer abstention **0.778**, answerable
  acceptance **0.576**. It rejects **36 of 85** genuine answerable questions,
  including "Where do I report a security incident?" and "Cần hỗ trợ VPN thì
  liên hệ ai?".
- The rejected answerable rows have empty or corpus-missing `gold_keywords`,
  i.e. the failures are exactly where the gold answer is an email address or an
  identifier rather than prose. The signal is tuned against a dataset whose
  answer format is too narrow to validate it on.

That is the substantive reason this is closed as a limitation: the obvious
cheap fixes were implemented and measured, and the one that works best still
loses 42% of correct answers. Adopting it would trade a visible abstention
defect for a quieter false-refusal defect.

## Decision

1. **Refusal to Tune Artificially**: We explicitly reject tuning `SIMILARITY_THRESHOLD` or relabelling evaluation datasets to present an artificial "100% pass" metric. Fabricating a metric obscures architectural reality and constitutes poor engineering practice.
2. **Decouple Retrieval from Verification**: We define a strict architectural separation:
   * **Stage 1 (Retrieval Filter)**: Dense + Sparse BM25 + Reciprocal Rank Fusion ($k=60$) measures *Topical Relevance* (does the corpus discuss this topic?).
   * **Stage 2 (Evidence Verification)**: A distinct decision layer measuring *Answerability* (does the retrieved passage actually entail/contain the specific information requested?).
3. **Documented Known Limitation**: Gate 8B is retained as `PARTIAL / MEASURED_LIMITATION` in the evaluation suite with published numbers, serving as transparent technical evidence.

## Next Phase Design (Target Architecture)

To achieve robust class separation without heuristic overfitting:
- **Lightweight NLI Entailment Classifier**: Cross-encoder model (e.g., MiniLM-L6-MNLI) evaluating `(passage, query)` premise-hypothesis pairs.
- **Span / Entity Verification Heuristic**: Rule/schema check verifying that the retrieved text contains expected answer entities (e.g., date, currency, policy clause numbers) before allowing LLM generation.

## Interview & Technical Defense Summary

> *"Cosine similarity measures semantic proximity in embedding space, not logical answerability. An unanswerable HR question retrieves HR policy chunks with ~0.70 similarity because they share vocabulary. Rather than tuning thresholds to fabricate clean metrics, we published the measured defect, documented the mathematical overlap in ADR-006, and decoupled topical retrieval from factual entailment verification."*
