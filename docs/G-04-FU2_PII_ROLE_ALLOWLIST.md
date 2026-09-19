# G-04-FU2 — PII role-email allowlist + contact-usability metric

Status: IMPLEMENTED 2026-09-07 — `ROLE_EMAIL_*` in `config.py:93-107`,
allowlist check in `loops/pii.py:is_role_email` (checked before `_EMAIL`),
golden `eval/golden/contact_usability.jsonl` (8 cases), metric
`contact_usability_rate` in `eval.py:_eval_row/evaluate_group`,
tests `tests/test_pii.py` (updated) + `tests/test_contact_usability.py` (new).
Refs: `src/maia/loops/pii.py:27`, `src/maia/pipeline_query.py:54-73`,
`src/maia/loops/guardrails.py:93-114`, `src/maia/eval.py:46-125`,
`tests/test_pii.py:17,68,76-79`, `eval/golden/`.

## 1. Problem
`PIIScanner` dùng 1 regex `_EMAIL` duy nhất, không phân biệt personal vs role email.
Repro: 7/7 role emails (`support@`, `it-help@`, `hr@`, `security@`, `benefits@`,
`eap@`, `onboarding@` @company.com) đều → `[PII-EMAIL]`. Câu hỏi "liên hệ ai"
retrieve đúng chunk nhưng answer vô dụng; `recall@k` không bắt được.

## 2. Scope (3 deliverables)
1. **Allowlist**: `ROLE_EMAIL_ALLOWLIST` (prefix `support|help|it-help|it-|hr|security|benefits|eap|onboarding`
   + exact set từ `data/enterprise/*.md` + `src/maia/ui_templates.py:172-173`).
   Check allowlist TRƯỚC `_EMAIL` trong `detect()`/`redact()`. Personal email giữ nguyên behavior.
2. **Golden mới**: `eval/golden/contact_usability.jsonl` (5-8 cases, format như `eval.py:8`):
   `{"question": "Cần hỗ trợ VPN thì liên hệ ai?", "gold_chunk_ids": [...],
   "gold_keywords": ["it-help@company.com"], "expect_contact": "it-help@company.com"}`.
   Cover VPN / laptop mất / leave / benefits / onboarding, cả VI + EN.
3. **Metric mới**: `contact_usability_rate` trong `_eval_row`/`evaluate_group`
   (answer sau redact còn chứa `expect_contact`?) — report cạnh `recall@k`,
   không thay đổi metric cũ.

## 3. Test plan
- Update `tests/test_pii.py`: role email → NOT flagged / NOT redacted;
  personal email → vẫn flagged (giữ các test cũ, sửa `test_detects_email`,
  `test_output_guardrail_redacts_pii_in_answer` dùng personal thay vì `hr@`).
- Mới: `tests/test_contact_usability.py` — end-to-end redact→answer còn contact.
- `evaluate_all` phải liệt kê `contact_usability` split.
- Acceptance: `recall@k` không giảm, `contact_usability_rate` từ ~0 → ~1.0,
  `pytest tests/test_pii.py tests/test_golden_eval.py` xanh.

## 4. Non-goals
- Không đổi pattern CCCD/phone/CC, không đụng BM25/RRF/rerank.
- Không auto-discover role email bằng LLM (static allowlist + config trước).
