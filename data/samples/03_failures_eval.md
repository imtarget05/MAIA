# RAG Failure Modes and Evaluation

## Failure 1 - Wrong chunk retrieved

Good LLM plus wrong context equals wrong answer. Fix with hybrid retrieval and reranking.

## Failure 2 - Bad context assembly

Too much irrelevant context reduces quality. Fix by limiting to top-3 chunks and 3000 chars.

## Failure 3 - Missing evidence

System must say "not enough evidence" when similarity is below 0.3 instead of hallucinating.

## Failure 4 - Chunking destroys meaning

Use SentenceSplitter with overlap to keep definitions together.

## Evaluation

Metrics: recall@k, hit@k, context precision, faithfulness, answer relevance.
Top-k retrieval balances recall vs noise. Top-3 final is the default.
