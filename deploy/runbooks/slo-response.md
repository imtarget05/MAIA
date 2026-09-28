# SLO Response — MAIA

Targets (`observability/slo.yaml`): grounding ≥ 0.95/30d, ingestion-lag ≤ 1000/7d, availability 0.99.

## Đo nhanh
- Availability: `up{job="maia"} == 0 for 2m` → incident.
- Grounding: `python src/maia/eval.py --group exact,paraphrase,no_answer` → `faithfulness_proxy`, `contact_in_ctx`.
- Latency: gateway `admin/stats` p95; api `/metrics` embed p95.

## Hành động theo SLO burn
1. Burn nhanh (2h window): chuyển `RERANK_MODE=fallback`, `MAIA_LLM_MODE=local`, scale API replicas.
2. Burn chậm (30d): bổ sung golden cases vào `eval/golden/`, chạy `eval_manifest.py`, review `SOP corpus`.
3. GhiEvidence: `eval/reports/slo-$(date +%F).json` + link trong PR.
