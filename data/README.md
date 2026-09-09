# Cấu trúc dữ liệu MAIA

Thư mục `data/` chứa kho tri thức nguồn cho pipeline RAG. Mọi file `.md` / `.txt` / `.pdf` trong thư mục sẽ được nạp nguyên thư mục bởi `ingest_data_dir()`.

## `samples/` — Dữ liệu demo (đơn giản)

Tài liệu giới thiệu chính MAIA và các khái niệm RAG. Dùng cho:
- Demo nhanh / eval offline (`PYTHONPATH=src python -m maia.cli ingest`)
- Kiểm thử pipeline mà không cần dữ liệu doanh nghiệp

| File | Nội dung |
|---|---|
| `01_maia_overview.md` | Tổng quan MAIA và 6 bước RAG pipeline |
| `02_vectors_hybrid.md` | Embeddings, Qdrant, hybrid search + RRF |
| `03_failures_eval.md` | RAG failure modes và metrics đánh giá |

## `enterprise/` — Kho tri thức doanh nghiệp (chuyên nghiệp, chi tiết)

Chính sách nội bộ của công ty theo chuẩn tài liệu doanh nghiệp. Mỗi tài liệu có:
- **Bảng metadata**: mã tài liệu (POL-XX-NNN), phiên bản, hiệu lực, phòng ban chủ trì, phạm vi áp dụng
- **Dòng "Từ khóa (VI)"**: phục vụ BM25 retrieval cho câu hỏi tiếng Việt
- **Bảng/số liệu cụ thể**: định mức VND, SLA, thời hạn, mức phê duyệt
- **Liên hệ đúng đầu mối**: HR ext 101, IT ext 202, Finance ext 303, InfoSec ext 205

| File | Mã | Nội dung |
|---|---|---|
| `HR_Policy.md` | POL-HR-001 | Giờ làm việc, chấm công, OT, thử việc, hợp đồng |
| `Leave_Policy.md` | POL-HR-002 | 12 ngày phép/năm, bảng thời hạn, luồng duyệt LV-YYYYMMDD-XXX |
| `Onboarding_Guide.md` | POL-HR-003 | Lịch ngày đầu, tài khoản, đào tạo bắt buộc, 30 ngày đầu |
| `Benefits.md` | POL-HR-004 | Bảo hiểm, thâm niên, L&D 10M/năm, gym 500k/tháng, thưởng |
| `IT_Security_Policy_v4.2.md` | POL-IT-001 | Mật khẩu, 2FA, MDM, mất thiết bị, báo cáo sự cố |
| `IT_Handbook.md` | POL-IT-002 | SLA P1/P2/P3, sửa laptop, cài phần mềm, chu kỳ thay thiết bị |
| `VPN_Guide.md` | POL-IT-003 | Xin VPN qua MAIA, cài đặt, bảng xử lý sự cố |
| `Expense_Policy.md` | POL-FIN-001 | Khoản hoàn, hạn 30 ngày, tạm ứng, công tác phí |

> **Lưu ý nhất quán dữ liệu**: các con số phải khớp giữa các tài liệu và mock HRIS
> (`src/maia/agent/hris.py`): 12 ngày phép/năm, mã yêu cầu `LV-YYYYMMDD-XXX`,
> VPN qua `create_it_ticket(type="vpn_request")`. Sửa chính sách → sửa cả mock HRIS
> và chạy lại `pytest tests/` trước khi commit.

## Cách nạp dữ liệu

```bash
# Nạp cả 2 thư mục vào Qdrant
PYTHONPATH=src python -m maia.cli ingest
PYTHONPATH=src python -m maia.cli ingest-enterprise

# Qua API
POST /ingest             # data/samples
POST /ingest/enterprise  # data/enterprise
POST /ingest/upload      # upload file tùy chọn
```

## Quy ước khi thêm tài liệu mới

1. Đặt tên file viết hoa theo chủ đề, gạch dưới phân từ (VD: `Remote_Work_Policy.md`).
2. Bắt đầu bằng tiêu đề `# Tên tài liệu — vX.Y` + bảng metadata + dòng "Từ khóa (VI)".
3. Mỗi section nên tự chứa được ngữ cảnh (chunk 512 ký tự có overlap 50) — nhắc lại mã tài liệu/tên chính sách trong section quan trọng.
4. Kết thúc bằng section "Liên hệ" có email + máy lẻ + Slack channel.
