# Hướng Dẫn Chuẩn Bị Jira Sandbox (Cho Task T2)

Tài liệu này hướng dẫn chuẩn bị môi trường **Jira Cloud Sandbox** hoàn toàn miễn phí (Free Tier lên đến 10 users, không cần thẻ tín dụng) để phục vụ việc tích hợp và kiểm thử cho **MAIA Service Desk (Task T2)**.

---

## 4 Thông số bắt buộc cần có:

1. **Site URL:** URL trang Jira của bạn (dạng: `https://<ten-workspace>.atlassian.net`)
2. **Email:** Email tài khoản Atlassian của bạn (dùng để xác thực API)
3. **API Token:** Token do Atlassian cấp (dạng `ATATT3...`)
4. **Project Key:** Mã viết hoa của Project trong Jira (ví dụ: `ITSD` hoặc `MAIA`)

---

## Các bước tạo trong 3 phút:

### Bước 1: Đăng ký Jira Cloud (Miễn phí 100%)
1. Truy cập: [https://www.atlassian.com/software/jira/free](https://www.atlassian.com/software/jira/free) (hoặc Jira Service Management Free).
2. Đăng ký bằng Google Workspace / Email cá nhân.
3. Chọn tên trang web (Site URL), ví dụ: `maia-servicedesk.atlassian.net`.
4. Hoàn tất thiết lập ban đầu (bỏ qua các câu hỏi khảo sát).

### Bước 2: Tạo Project & lấy Project Key
1. Tại màn hình Jira, bấm **Projects** > **Create project**.
2. Chọn template:
   - **Service project** (hoặc **Kanban** / **Scrum** / **Task tracking** thuộc Business/Software).
3. Đặt tên Project, ví dụ: `MAIA Service Desk`.
4. Đặt **Key**: ví dụ `ITSD` hoặc `MAIA`.
5. Bấm **Create project**. Lưu lại mã **Key** này.

### Bước 3: Tạo Atlassian API Token
1. Đăng nhập vào trang quản lý Atlassian API Tokens:
   👉 [https://id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Bấm **Create API token**.
3. Đặt nhãn (Label), ví dụ: `MAIA-T2-Sandbox`.
4. Bấm **Create** và copy ngay token được tạo (chuỗi bắt đầu bằng `ATATT3...`).
   *(Lưu ý: Token chỉ hiển thị 1 lần, hãy lưu vào nơi an toàn)*.

---

## Kiểm tra kết nối tự động bằng Script

Trong repo MAIA đã chuẩn bị sẵn công cụ kiểm tra tự động tại `scripts/verify_jira_sandbox.py`.

### Cách 1: Chạy kiểm tra trực tiếp qua CLI
```bash
python scripts/verify_jira_sandbox.py \
  --url https://<ten-workspace>.atlassian.net \
  --email <email-cua-ban> \
  --token <api-token-vua-copy> \
  --project ITSD
```

### Cách 2: Cấu hình vào file `.env.servicedesk`
1. Copy file mẫu:
   ```bash
   cp .env.servicedesk.example .env.servicedesk
   ```
2. Điền các thông số vào `.env.servicedesk`:
   ```ini
   SD_JIRA_BASE_URL=https://<ten-workspace>.atlassian.net
   SD_JIRA_EMAIL=<email-cua-ban>
   JIRA_API_TOKEN=<api-token-vua-copy>
   SD_JIRA_PROJECT_KEY=ITSD
   ```
3. Chạy script:
   ```bash
   python scripts/verify_jira_sandbox.py
   ```

Script sẽ tự động:
- Kiểm tra xác thực Basic Auth qua `/rest/api/3/myself`.
- Kiểm tra quyền truy cập Project qua `/rest/api/3/project/{project_key}`.
- Lấy danh sách Issue types hỗ trợ tạo ticket.
- Báo kết quả **READY** kèm cấu hình mẫu để báo cáo.
