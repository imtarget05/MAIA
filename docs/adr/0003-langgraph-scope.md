# ADR-0003 — LangGraph for HITL actions, not for plain RAG Q&A

Status: accepted (2026-09-28).

Context: temptation to route everything through LangGraph ("agent washing").

Decision: plain policy Q&A stays a deterministic pipeline
(`pipeline_query.py`: retrieve → gate → rerank → generate → grounding/PII
checks). LangGraph (`agent/langgraph_agent.py`, StateGraph + SqliteSaver) is
used only for side-effect actions requiring interrupt/approve/resume.

Tradeoffs: two paths to maintain, but the Q&A path stays debuggable and cheap,
while approvals get durable checkpoints. No multi-agent layer: the workload
(router → research → action → validate) does not justify the cost/latency.

