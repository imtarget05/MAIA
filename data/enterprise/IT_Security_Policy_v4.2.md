# Chính sách An toàn Thông tin — IT Security Policy v4.2

| Thuộc tính | Giá trị |
|---|---|
| Mã tài liệu | POL-IT-001 |
| Phiên bản | 4.2 |
| Hiệu lực | 01/07/2024 |
| Chủ trì | Bộ phận An toàn Thông tin (InfoSec) |
| Phạm vi áp dụng | Toàn thể nhân viên, cộng tác viên, nhà thầu có tài khoản hệ thống nội bộ |

> **Từ khóa (VI):** bảo mật, an toàn thông tin, mật khẩu, xác thực 2FA, phishing, dữ liệu nhạy cảm, mất thiết bị, security policy

---

## 1. Chính sách Mật khẩu

- Độ dài tối thiểu **12 ký tự**, gồm chữ hoa + chữ thường + số + ký tự đặc biệt.
- Bắt buộc đổi mật khẩu mỗi **90 ngày**; không được tái sử dụng 5 mật khẩu gần nhất.
- Ưu tiên dùng **SSO công ty** cho mọi dịch vụ có hỗ trợ; kích hoạt **2FA** bắt buộc cho email, GitLab, VPN và HR portal.
- Không lưu mật khẩu trong file/ghi chú không mã hóa; dùng password manager được công ty cấp (1Password Teams).

## 2. Quản lý thiết bị

- Mọi laptop công ty phải bật **mã hóa ổ đĩa** (FileVault/BitLocker) và cài **MDM** (Fleet) ngay khi nhận thiết bị.
- Cấm cài phần mềm crack, phần mềm không qua duyệt của IT Security.
- Cập nhật hệ điều hành trong vòng **14 ngày** kể từ khi bản vá bảo mật quan trọng được phát hành.

## 3. Quy trình khi Mất / Bị đánh cắp thiết bị

1. **Báo ngay trong vòng 1 giờ** kể từ khi phát hiện, qua IT Help Desk: `it-help@company.com` | máy lẻ **202** | Slack `#it-help`.
2. IT sẽ **khoá từ xa toàn bộ thiết bị**, thu hồi token truy cập, reset mật khẩu SSO.
3. IT cấp thiết bị thay thế trong vòng **2 ngày làm việc**.
4. Không báo cáo đúng hạn sẽ bị rà soát bảo mật (security review) và có thể xử lý kỷ luật.

## 4. Truy cập VPN

- VPN **bắt buộc** khi truy cập dịch vụ nội bộ từ bên ngoài: Confluence, Jira, GitLab, HR portal.
- Xin cấp VPN qua MAIA ("cách request VPN") hoặc IT portal — duyệt tự động với nhân viên đang hoạt động. Chi tiết cài đặt tại `VPN_Guide.md`.
- Cấm chia sẻ thông tin đăng nhập VPN; ngắt kết nối khi không sử dụng.

## 5. Báo cáo Sự cố Bảo mật

- Gửi ngay tới `security@company.com`, subject format: `[SEC-INCIDENT] <ngắn gọn>`.
- Ví dụ sự cố: nhận email nghi phishing, phát hiện dữ liệu khách hàng lộ, thiết bị bất thường truy cập tài khoản.
- Không tự xử lý / xóa dấu vết trước khi InfoSec xác nhận. Phản hồi ban đầu trong **30 phút** giờ làm việc.
- Chính sách **không truy cứu** đối với người báo cáo sự cố do vô tình.

## 6. Đào tạo & Tuân thủ

- Khóa **Security Awareness** bắt buộc mỗi 6 tháng qua LMS; tỷ lệ hoàn thành ≥ 95% theo phòng ban.
- Giả lập phishing hàng quý; nhân viên click link giả lập 3 lần liên tiếp sẽ được huấn luyện lại 1-1.

## 7. Liên hệ

- InfoSec: `security@company.com` | máy lẻ **205** | Slack `#security`
- IT Help Desk: `it-help@company.com` | máy lẻ **202** | Slack `#it-help`
