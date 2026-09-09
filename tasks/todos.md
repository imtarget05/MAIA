# Deferred Goal Ledger

> **Status**: Backlog
> **Updated**: (initial)
> **Scope**: Medium/long-term goals deferred from active plan execution

Current plan tasks live in the active plan's `## Task Breakdown`.
Do not duplicate that execution checklist here. Record only work intentionally deferred beyond this slice, with the tradeoff and revisit trigger.

## Deferred Goals

| Goal | Why Deferred | Tradeoff | Revisit Trigger |
|------|--------------|----------|-----------------|
| ~~Merge `OutputValidator` into `OutputGuardrail`~~ **DONE (WS5, 2026-09-09)** — merged; `is_action_response=True` chỉ còn ở action confirm path (hợp lệ) | — | — | — |
| ~~Re-evaluate stream/CRAG/LTM untangle~~ **DONE (WS6, 2026-09-09)** — import-time deps cut (lazy), `ltm_learn` opt-in qua `LTM_LEARN_ON_CHAT`; **scope decision: retain cả ba opt-in** | — | — | Flip `LLAMA_INDEX_DATA_PLANE=True` sau CI head-to-head eval (WS2); enable-by-default bất kỳ module nào chỉ khi eval ổn |
| ~~Flip `LLAMA_INDEX_DATA_PLANE` default True~~ **DONE (WS2, 2026-09-10)** — head-to-head eval trên Qdrant Cloud (FastEmbed thật, 8 docs/25 chunks, top-k=3, 10 golden groups): hit@k / recall@k / context_precision / mrr **đồng nhất 100%** dense vs hybrid ở mọi group → default flipped True (`config.py:46`); legacy-path tests pin flag OFF qua autouse fixture | — | — | Revert nếu CI retrieval-regression báo regression |
