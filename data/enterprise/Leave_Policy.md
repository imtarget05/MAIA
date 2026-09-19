# Chính sách Nghỉ phép — Leave Policy v2.4

| Thuộc tính | Giá trị |
|---|---|
| Mã tài liệu | POL-HR-002 |
| Phiên bản | 2.4 |
| Hiệu lực | 01/01/2024 |
| Chủ trì | Phòng Nhân sự (HR) |
| Phạm vi áp dụng | Toàn thể nhân viên |

> **Từ khóa (VI):** nghỉ phép, ngày phép, xin nghỉ phép, phép năm, nghỉ ốm, nghỉ thai sản, annual leave, sick leave, leave request, carry over

---

## 1. Định mức nghỉ phép

- Phép năm: **12 ngày làm việc mỗi năm dương lịch** cho mỗi nhân viên.
- Chuyển phép (carry over): tối đa **3 ngày** chưa dùng sang năm sau, phải được quản lý duyệt trước 31/12.
- Nghỉ ốm: tách riêng với phép năm, tối đa **30 ngày/năm** khi có giấy xác nhận của bác sĩ (bắt buộc với nghỉ ốm từ 2 ngày liên tiếp trở lên).

## 2. Thời hạn nộp yêu cầu

| Số ngày nghỉ | Nộp trước | Phê duyệt |
|---|---|---|
| 1–2 ngày | 3 ngày làm việc | Quản lý trực tiếp |
| 3–5 ngày | 7 ngày làm việc | Quản lý trực tiếp |
| Trên 5 ngày | 14 ngày làm việc | Quản lý + Giám đốc Nhân sự |

## 3. Luồng phê duyệt (Approval Flow)

1. Nhân viên tạo yêu cầu nghỉ phép qua MAIA ("xin nghỉ phép 2 ngày từ 10/09") hoặc HR portal, ghi rõ ngày bắt đầu — ngày kết thúc — số ngày.
2. Hệ thống kiểm tra số dư phép qua `check_leave_balance`. Không đủ số dư → yêu cầu bị từ chối tự động.
3. Quản lý duyệt / từ chối trong vòng **48 giờ**. Quá hạn sẽ tự động nhắc lại và escalate.
4. HR xác nhận và cập nhật số dư. Hệ thống trả về **Mã Yêu cầu (Request ID)** định dạng `LV-YYYYMMDD-XXX` (ví dụ: `LV-20260910-001`).

## 4. Tra cứu số dư

- Hỏi MAIA: **"số ngày phép còn lại của tôi?"** — hệ thống truy vấn database HR và trả về số ngày còn lại ngay lập tức.
- Hoặc tự tra trên HR portal → mục Leave Balance.

## 5. Nghỉ phép đặc biệt

- Thai sản (nữ): 6 tháng theo BHXH; nam: 10 ngày có lương khi vợ sinh.
- Ma chay (cha, mẹ, vợ/chồng, con): 3 ngày có lương.
- Nghỉ không lương: tối đa 30 ngày/năm, cần duyệt của quản lý + HR.
- Các trường hợp trên **không trừ** vào phép năm.

## 6. Liên hệ

- HR Help Desk: `hr@company.com` | máy lẻ **101** | Slack `#hr-help`
