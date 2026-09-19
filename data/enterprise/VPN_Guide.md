# Hướng dẫn VPN — VPN Guide v1.8

| Thuộc tính | Giá trị |
|---|---|
| Mã tài liệu | POL-IT-003 |
| Phiên bản | 1.8 |
| Hiệu lực | 01/07/2024 |
| Chủ trì | Bộ phận IT / InfoSec |
| Phạm vi áp dụng | Nhân viên truy cập hệ thống nội bộ từ xa |

> **Từ khóa (VI):** VPN, mạng riêng ảo, xin cấp VPN, yêu cầu VPN, truy cập từ xa, remote access, kết nối VPN, mạng công ty

---

## 1. VPN là gì

VPN cho phép truy cập **an toàn** vào mạng nội bộ công ty từ bên ngoài (nhà, công tác phí, quán cà phê). Theo Chính sách An toàn Thông tin v4.2, VPN là **bắt buộc** khi truy cập: Confluence, Jira, GitLab, HR portal.

## 2. Cách xin cấp VPN

1. Hỏi MAIA trực tiếp: **"cách request VPN"** — MAIA tạo ticket tự động qua `create_it_ticket(type="vpn_request")`.
2. Hoặc tự đặt ticket tại IT portal: `vpn.company.com`, cung cấp **mã nhân viên** và lý do công việc.
3. Phê duyệt: **tự động** với nhân viên đang hoạt động, trong vòng **2 giờ** giờ làm việc.
4. Khi có ticket, tài khoản VPN được cấp kèm file cấu hình và gửi về email công ty.

## 3. Cài đặt

1. Tải client tại `vpn.company.com/download` (hỗ trợ macOS, Windows, Linux, iOS, Android).
2. Cài đặt, đăng nhập bằng **SSO công ty**.
3. File cấu hình được cấp phát tự động (auto-provisioned) — không cần nhập thủ công.
4. Kết nối thử tới `confluence.company.com` để xác nhận; nếu mở được là VPN hoạt động.

## 4. Xử lý sự cố thường gặp

| Lỗi | Nguyên nhân | Cách xử lý |
|---|---|---|
| `Authentication failed` | Token SSO hết hạn | Đăng nhập lại SSO; nếu vẫn lỗi, reset token tại `vpn.company.com/reset` |
| Kết nối chậm | Server node quá tải | Chọn node khác trong menu (SG-1, SG-2, HCM-1) |
| Không kết nối được | Chặn mạng (khách sạn, quán cà phê) | Thử port 443 (TCP fallback) trong Settings |
| Mất mạng sau khi ngắt VPN | DNS cache cũ | Flush DNS hoặc khởi động lại client |

- Vẫn không được: nhắn `#it-help` kèm screenshot lỗi và thời điểm xảy ra.

## 5. Bảo mật khi dùng VPN

- **Cấm chia sẻ** thông tin đăng nhập VPN cho bất kỳ ai, kể cả đồng nghiệp.
- Ngắt kết nối VPN khi không sử dụng.
- Không dùng VPN công ty trên thiết bị cá nhân chưa đăng ký MDM.

## 6. Liên hệ

- IT Help Desk: `it-help@company.com` | máy lẻ **202** | Slack `#it-help`
