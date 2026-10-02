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
