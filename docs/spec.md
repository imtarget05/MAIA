# Product Spec: MAIA — Nền tảng tri thức RAG thông minh

> **Trạng thái**: Draft
> **Cập nhật lần cuối**: 2026-09-08
> **Chủ sở hữu**: Planner

## Kết quả sản phẩm

MAIA là trợ lý nội bộ doanh nghiệp, trả lời câu hỏi chính sách HR/IT/bảo mật dựa trên kho tài liệu công ty. Mọi câu trả lời phải có căn cứ trích dẫn — không suy đoán. Hành động có side-effect (xin nghỉ phép, tạo ticket IT) luôn cần duyệt trước khi thực hiện.

## Tiêu chí thành công

- **Workflow chính:** Nhân viên nhập câu hỏi → MAIA **Tìm → Hiểu → Trích dẫn → Hành động** → trả lời có căn cứ, hoặc từ chối trung thực, hoặc yêu cầu làm rõ, hoặc đề xuất hành động cần duyệt.
- **Thanh chất lượng:**
  - `recall@k` không giảm so với baseline (baseline FastEmbed: `hit@k=1.0`, `recall@k=1.0`, `ctx_prec=0.764`, intent `10/10` — `eval/baseline_bge-small-en-v1.5.json`).
  - `contact_usability_rate` ~1.0 ở split `contact_usability` (sau redact PII, câu trả lời "liên hệ ai" vẫn chứa email bộ phận dùng được — G-04-FU2).
  - 128 tests offline xanh (`MAIA_EMBED_FORCE_HASH=1`, không cần Qdrant/Kafka).
- **Ngoài phạm vi:**
  - Không tự phát hiện role email bằng LLM (chỉ allowlist tĩnh).
  - Không phát hiện SSN Mỹ (chỉ email, SĐT VN, CCCD/CMND, thẻ tín dụng).
  - Không đụng BM25/RRF/rerank trong scope này.

## Ràng buộc

- **Offline-first:** Chạy đầy đủ ở chế độ mock khi không có Cloudflare creds. Embedding dùng hash fallback khi FastEmbed thiếu (`MAIA_EMBED_FORCE_HASH=1`).
- **Multi-tenant:** Mọi query/chat/stream đều nhận `tenant_id`; Qdrant filter theo payload `tenant_id`. Mặc định `default`.
- **C1 — Confirm-before-action:** Side-effect chỉ chạy ở `POST /actions/confirm`. Chat trả về `needs_approval` + `pending_action`, KHÔNG thực hiện ngay.
- **PII 2 lớp:** Ingest-time (`pipeline_query._ingest_rawdocs`) + output guardrail (`loops/guardrails.py`), quét email/SĐT/CCCD/CC trước khi embed và trước khi trả lời.
- **Liên hệ bộ phận sống sót sau redact:** Email vai trò (`it-help@`, `hr@`, `security@`, ... @company.com) được bypass qua allowlist, không bị `[PII-EMAIL]` (`src/maia/loops/pii.py:80-97`).

## Kịch bản chấp nhận

1. **Trả lời có căn cứ:**
   - *Cho:* Nhân viên hỏi "Chính sách nghỉ phép cho phép b nhiêu ngày?"
   - *Khi:* MAIA retrieve chunk từ `Leave_Policy.md`, rerank, tạo answer với `[S1]` trích dẫn.
   - *Thì:* `status = "answered"`, `citations[]` không rỗng, `has_evidence = true`. Ref: `src/maia/agent/schemas.py:24-32`.

2. **Từ chối trung thực:**
   - *Cho:* Nhân viên hỏi thông tin ngoài kho tri thức.
   - *Khi:* MAIA retrieve chunk nhưng dense score < ngưỡng evidence.
   - *Thì:* `status = "insufficient_evidence"`, `has_evidence = false`, answer nói rõ "không tìm thấy căn cứ", KHÔNG đoán. Ref: `src/maia/agent/agent.py:39`.

3. **Xin làm rõ (slot filling):**
   - *Cho:* Nhân viên nói "Tôi muốn xin nghỉ phép" (thiếu số ngày + ngày bắt đầu).
   - *Khi:* Rule-based intent detect `leave_request`, slots `days` + `start_date` thiếu.
   - *Thì:* `status = "needs_clarification"`, MAIA hỏi "Bạn muốn nghỉ bao nhiêu ngày, từ ngày nào?", chưa retrieval. Ref: `src/maia/agent/intents.py`, `src/maia/agent/session.py:38`.

4. **Duyệt trước khi làm (C1):**
   - *Cho:* Nhân viên đã đủ slot: "Tôi muốn xin nghỉ 5 ngày từ 10/09/2026".
   - *Khi:* MAIA tạo `pending_action` với type + params + summary.
   - *Thì:* `status = "needs_approval"`, UI hiện thẻ "Đồng ý / Thôi", side-effect chỉ chạy khi bấm "Đồng ý" gọi `POST /actions/confirm`. Ref: `src/maia/agent/schemas.py:81-84`, `src/maia/agent/agent.py:490-512`.

5. **Contact dùng được sau PII redact:**
   - *Cho:* Nhân viên hỏi "Cần hỗ trợ VPN thì liên hệ ai?"
   - *Khi:* Chunk chứa `it-help@company.com` được retrieve; PII scanner quét email.
   - *Thì:* Email `it-help@company.com` KHÔNG bị redact (role email allowlist). Email cá nhân vẫn thành `[PII-EMAIL]`. Answer cuối chứa email bộ phận dùng được. Ref: `src/maia/loops/pii.py:80-97`, `docs/G-04-FU2_PII_ROLE_ALLOWLIST.md`.

## Câu hỏi mở (đã đóng 2026-09-09, slice Spec-Alignment Hardening)

1. ~~Điểm `en_policy = 0.0` trong bảng eval README (`recall@k=0.0`): đây là do hash-mode hay thiếu dữ liệu EN?~~ → **Hash-mode, không thiếu dữ liệu.** Thí nghiệm đối đầu (cùng corpus enterprise + `HybridRetriever`, chỉ đổi embedder): hash `hit@k=0.6/recall@k=0.55`, FastEmbed `1.0/1.0`. Chuỗi nhân quả: hash embedder cho cosine ≈ 0 → evidence gate (`top_dense >= 0.3`, `pipeline_query.py:58-59`) từ chối → `citations=[]` → eval đo 0.0. Chi tiết: `docs/researches/en-recall-investigation.md`. Không hạ threshold để "fix" số hash-mode.
2. ~~`contact_usability_rate` có nên trở thành gate CI không?~~ → **Có, threshold 1.0 trên metric conditional.** Gate chạy ở job `retrieval-regression` (có Qdrant + enterprise docs): `maia.eval --group contact_usability --top-k 3 --fail-under-contact 1.0`. Gate kiểm tra `contact_context_rate` **conditional** — trong các row retrieve được (`hit==1`), role email có sống sót sau redact và tới được context không; retrieval miss (`hit==0`) không mang tín hiệu redact và bị loại khỏi mẫu (đo 4/4 = 1.0 ngày 2026-09-09). Metric answer-level `contact_usability_rate` giữ nguyên (trung thực end-to-end) nhưng chỉ có ý nghĩa khi có LLM thật (mock LLM không echo context).
3. ~~Spec có cần mở rộng cho Kafka streaming (Project 2) và CRAG opt-in không?~~ → **Không trong v1.** Kafka streaming, multi-agent teams, voice, finetune: archived (`_archive/`). CRAG/LTM: giữ nhưng mặc định OFF, ngoài phạm vi v1. Mở rộng scope (v2) là slice riêng, xem `tasks/todos.md`.

## Phạm vi v1 (locked)

Trong phạm vi: Find → Hiểu → Trích dẫn → Hành động (4 kịch bản chấp nhận trên), C1, PII 2 lớp + role allowlist, offline-first mock, multi-tenant. Ngoài phạm vi: Kafka streaming, CRAG opt-in, teams, voice, finetune, LTM cross-session (mặc định OFF).
