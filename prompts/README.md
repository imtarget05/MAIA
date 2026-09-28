# Prompt Library (PromptOps)

Thư mục này là **nguồn sự thật duy nhất** cho mọi prompt của MAIA. Prompt không nằm
trong code Python: chúng là artefact có version, có review, có eval — giống migration
DB hay API schema.

## Quy ước bắt buộc

| Quy ước | Lý do |
|---|---|
| Tên file `<name>.v<major>.<minor>.<patch>.yaml` | Loader từ chối file sai tên; nhìn thư mục là biết có bao nhiêu bản |
| `name` + `version` trong file phải khớp tên file | Chặn lỗi copy-paste: hai nội dung khác nhau dưới cùng một version |
| `system_prompt`, `user_template`, `parameters`, `output_schema` | Đủ để tái lập hành vi mà không cần đọc code gọi prompt |
| `temperature` 0–2, `top_p` 0–1, `max_tokens` > 0 | Lỗi typo bị chặn lúc load, không lọt ra production |
| `parameters.rationale` khi giá trị khác mặc định | Reviewer phải biết *vì sao* 0.8 chứ không phải 0.2 |
| `guardrails` chỉ dùng rule trong `maia.promptops.guardrails.GUARDRAIL_RULES` | Rule lạ = prompt bị từ chối, không "guard" rỗng |
| `eval_cases` có `id` duy nhất | Case trùng id làm báo cáo eval không đọc được |
| `status: draft` cho bản đang review | Runtime chỉ chọn bản `active`; draft không thể lọt ra ngoài |

## Vòng đời một thay đổi prompt

1. **Sửa**: tạo file version mới (không sửa file đã release — sửa tại chỗ làm mất khả năng
   rollback và làm báo cáo eval cũ trở thành vô nghĩa).
2. **Khai báo `supersedes`** để biết bản nào thay bản nào.
3. **Chạy eval offline** (golden case, không cần model): `pytest tests/test_prompt_library.py -q`.
4. **Chạy eval có model** khi có gateway (tuỳ chọn): `python -m maia.promptops_cli run-evals`.
5. **Review như PR code**: dùng `PromptRegistry.diff()` để xem field nào đổi — diff này
   là nội dung review, không phải ảnh chụp màn hình.
6. **Chuyển `status: active`** khi eval xanh.

## Vì sao có case "golden answer"?

Khoảng 1/3 eval case trong thư viện này có trường `answer` (golden). Các case đó kiểm tra
**hợp đồng** — schema, thứ tự biến, guardrail, parser JSON — và chạy được **không cần model**.
Đây là bằng chứng trung thực ở mức "hạ tầng prompt hoạt động", KHÔNG phải bằng chứng
"model trả lời hay". Case không có `answer` cần model thật và chỉ chạy khi có gateway.
Báo cáo eval ghi rõ `golden: true/false` cho từng case để không ai nhầm lẫn hai loại này.

## Cấu trúc

```
prompts/
├── marketing/    campaign_copywriter@{1.0.0,1.1.0}    # phễu nội dung đa kênh
├── product/      game_review_insight@1.0.0            # insight từ review người chơi
├── analytics/    nl_to_sql@1.0.0                      # hỏi đáp dữ liệu (SELECT-only)
└── operations/   retention_drop_briefing@1.0.0        # briefing cảnh báo KPI
```

## Chạy nhanh

```python
from maia.promptops import default_registry, render

reg = default_registry()
spec = reg.require("campaign_copywriter", "^1.1.0")      # bản active mới nhất trong major 1
print(reg.diff("campaign_copywriter@1.0.0", spec.ref).summary())
prompt = render(spec, {"game_name": "...", ...})
```
