# Hướng dẫn Deploy Production — MAIA

> **Phiên bản**: 1.0  
> **Cập nhật**: 2026-09-11  
> **Đối tượng**: Ops / DevOps / Team MAIA

---

## Mục lục

1. [Prerequisites](#1-prerequisites)
2. [Qdrant Cloud Setup](#2-qdrant-cloud-setup)
3. [Cloudflare Workers AI Setup](#3-cloudflare-workers-ai-setup)
4. [Render Backend Deployment](#4-render-backend-deployment)
5. [Streamlit Cloud Frontend Deployment](#5-streamlit-cloud-frontend-deployment)
6. [First Ingestion](#6-first-ingestion)
7. [Production Checklist](#7-production-checklist)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites

Trước khi bắt đầu, chuẩn bị các tài khoản sau:

| Dịch vụ | Mục đích | Chi phí |
|---------|---------|---------|
| GitHub | Lưu trữ mã nguồn | Free |
| Render | Host FastAPI backend | Free tier |
| Streamlit Community Cloud | Host Streamlit frontend | Free (always-on) |
| Qdrant Cloud | Vector DB | Free 1GB cluster |
| Cloudflare | LLM Workers AI | Free tier |

**Yêu cầu chung:**
- Mã nguồn đã đẩy lên GitHub repo công khai/private.
- Đã cài `git` và có quyền push.
- Có quyền tạo secret/env var trên từng nền tảng.

---

## 2. Qdrant Cloud Setup

### 2.1 Tạo cluster

1. Đăng nhập [cloud.qdrant.io](https://cloud.qdrant.io).
2. **Create Cluster** → chọn **Free** (1GB, 1 node).
3. Đặt tên cluster, ví dụ: `maia-knowledge`.
4. Chọn region gần nhất với Render (ví dụ: `eu-central-1` Frankfurt).

### 2.2 Lấy thông tin kết nối

Sau khi cluster sẵn sàng, ở trang **Cluster Details**:

```
QDRANT_URL = https://<cluster-id>.gcp.cloud.qdrant.io
QDRANT_API_KEY = <your-api-key>
```

### 2.3 Tạo payload indexes (bắt buộc)

MAIA dùng multi-tenant filter + BM25 hybrid; cần index để query nhanh:

```bash
# Cài Qdrant CLI nếu chưa có
pip install qdrant-client

# Tạo indexes
python - <<'PY'
from qdrant_client import QdrantClient

url = "https://<cluster-id>.gcp.cloud.qdrant.io"
key = "<your-api-key>"
c = QdrantClient(url=url, api_key=key)

c.create_payload_index(
    collection_name="maia_knowledge",
    field_name="tenant_id",
    field_schema="keyword",
)
c.create_payload_index(
    collection_name="maia_knowledge",
    field_name="doc_id",
    field_schema="keyword",
)
c.create_payload_index(
    collection_name="maia_knowledge",
    field_name="chunk_id",
    field_schema="keyword",
)
print("Payload indexes created.")
PY
```

### 2.4 Verify kết nối

```bash
python -c "from qdrant_client import QdrantClient; c=QdrantClient(url='<QDRANT_URL>', api_key='<QDRANT_API_KEY>'); print('Points:', c.count('maia_knowledge').count)"
```

---

## 3. Cloudflare Workers AI Setup

### 3.1 Lấy Account ID

1. Đăng nhập [dash.cloudflare.com](https://dash.cloudflare.com).
2. Chọn **Workers & Pages** → bên phải có **Account ID** (dạng UUID).
3. Copy `CLOUDFLARE_ACCOUNT_ID`.

### 3.2 Tạo API Token

1. Vào **My Profile** → **API Tokens** → **Create Token**.
2. Chọn **Edit Cloudflare Workers** (template) hoặc custom:
   - Account: `Workers AI: Edit`
3. Đặt tên token, ví dụ: `maia-workers-ai`.
4. Tạo và copy token ngay (chỉ hiện 1 lần).

> **KHÔNG nhầm với R2 token (`cfat_...`):** token R2 chỉ có quyền object
> storage, KHÔNG gọi được Workers AI (`/ai/run/...` sẽ 403). Token Workers AI
> phải có quyền **Account → Workers AI → Edit**. Token lộ trong chat/log
> phải **rotate ngay** (Dashboard → API Tokens → Roll/Delete) — KHÔNG bao giờ
> paste token thật vào code, `.env.example`, docs hay git.

```
CLOUDFLARE_API_TOKEN = <your-token>
CLOUDFLARE_MODEL = @cf/meta/llama-3.1-8b-instruct
```

### 3.3 Verify LLM

```bash
curl "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/run/@cf/meta/llama-3.1-8b-instruct" \
  -H "Authorization: Bearer <CLOUDFLARE_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Hello, world!"}'
```

Nếu trả về JSON có `response`, credential hợp lệ.

---

## 4. Render Backend Deployment

Repo đã có `render.yaml` (Blueprint). Render sẽ auto-detect file này khi push lên GitHub.

### 4.1 Push code lên GitHub

```bash
git add .
git commit -m "chore: prepare for production deploy"
git push origin main
```

### 4.2 Tạo service trên Render

**Cách A: Auto-deploy qua Blueprint**

1. Vào [render.com](https://render.com) → **New** → **Blueprint**.
2. Chọn repo GitHub của MAIA.
3. Render đọc `render.yaml` → tự động tạo 2 services: `maia-api` và `maia-ui`.
4. Ở bước **Environment Variables**, Render sẽ yêu cầu điền các giá trị `sync: false`.

**Cách B: Tạo thủ công**

1. **New** → **Web Service** → chọn repo.
2. **Runtime**: Python 3.
3. **Plan**: Free.
4. **Build Command**:
   ```bash
   pip install --upgrade pip && pip install -r requirements.txt
   ```
5. **Start Command**:
   ```bash
   cd /opt/render/project/src && PYTHONPATH=/opt/render/project/src uvicorn maia.api:app --host 0.0.0.0 --port $PORT
   ```
6. **Health Check Path**: `/health`

### 4.3 Điền Environment Variables

Vào **Settings** → **Environment** → thêm các biến sau:

| Biến | Giá trị | Ghi chú |
|------|---------|---------|
| `PYTHON_VERSION` | `3.12` | Render auto-detect |
| `ENVIRONMENT` | `production` | Bắt buộc |
| `STORAGE_DIR` | `/tmp/storage` | Render disk ephemeral |
| `DATA_DIR` | `/opt/render/project/src/data/samples` | Read-only |
| `ENTERPRISE_DATA_DIR` | `/opt/render/project/src/data/enterprise` | Read-only |
| `LLAMA_INDEX_DATA_PLANE` | `true` | Default đã flip True sau WS2 |
| `DEFAULT_EMPLOYEE_ID` | `emp_001` | |
| `BOOTSTRAP_FIRST_ADMIN` | `true` | Tự động promote user đầu tiên thành admin |
| `JWT_SECRET_KEY` | `<random-64-char>` | **BẮT BUỘC**. Tạo bằng: `openssl rand -hex 32` |
| `CORS_ORIGINS` | `https://maia-ui.onrender.com` | Domain frontend sau khi deploy |
| `API_BASE_URL` | `https://maia-api.onrender.com` | URL backend công khai |
| `APP_BASE_URL` | `https://maia-ui.onrender.com` | URL frontend (dùng trong email reset) |
| `QDRANT_URL` | `https://<cluster>.gcp.cloud.qdrant.io` | Từ bước 2.2 |
| `QDRANT_API_KEY` | `<qdrant-api-key>` | Từ bước 2.2 |
| `QDRANT_COLLECTION` | `maia_knowledge_v2` | 1024-dim (BGE-m3), khớp `render.yaml` |
| `CLOUDFLARE_ACCOUNT_ID` | `<account-id>` | Từ bước 3.1 (chuỗi hex 32 ký tự trên Dashboard) |
| `CLOUDFLARE_API_TOKEN` | `<api-token>` | Từ bước 3.2 |
| `CLOUDFLARE_MODEL` | `@cf/meta/llama-3.1-8b-instruct` | |
| `WORKFLOW_DB_PATH` | `/tmp/storage/workflow.db` | |
| `SESSION_DB_PATH` | `/tmp/storage/session.db` | |

> **Lưu ý**: Render free tier có disk **ephemeral** — file trong `/tmp/storage` sẽ mất khi service restart. Điều này ảnh hưởng BM25 cache, session DB. Xem [Troubleshooting](#bm25-cache-rebuilding) để biết cách xử lý.

> **CẢNH BÁO — Render `PUT /env-vars` REPLACE toàn bộ env set** (đã gây sập
> production 2026-09-11: PUT thiếu key làm mất `JWT_SECRET_KEY`/
> `QDRANT_API_KEY`/`CLOUDFLARE_API_TOKEN`, service crash-loop). Cập nhật env
> qua **Dashboard → Settings → Environment** (UI merge từng key, an toàn), hoặc
> nếu dùng API thì luôn gửi **full-set** cả 2 services (`maia-api` + `maia-ui`).

### 4.4 Deploy

Sau khi lưu env vars, Render tự động trigger build + deploy.

Theo dõi log ở tab **Logs**. Build mất ~2-5 phút (install dependencies).

### 4.5 Verify backend

```bash
# Thay <RENDER_API_URL> bằng URL thật, ví dụ: https://maia-api.onrender.com
curl https://<RENDER_API_URL>/health   # liveness: {"status":"ok","version":"..."}
curl https://<RENDER_API_URL>/ready    # readiness: Qdrant + llm_mode + embed_model
```

Expected response của `GET /ready` khi Workers AI đã cấu hình đúng:

```json
{
  "status": "ok",
  "qdrant_points": 25,
  "collection": "maia_knowledge_v2",
  "llm_mode": "cloudflare",
  "rerank_mode": "score-fallback",
  "embed_model": "@cf/baai/bge-m3"
}
```

`llm_mode` phải là `"cloudflare"` — nếu là `"mock"` thì
`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN` chưa tới được service (kiểm tra
Render Dashboard env). Kiểm tra local trước khi deploy:

```bash
python3 scripts/check_workers_ai.py          # mock-safe, không gọi mạng
python3 scripts/check_workers_ai.py --live   # probe thật (cần creds thật)
```

Nếu `status` = `degraded`, kiểm tra `error` trong response và xem [Troubleshooting](#85-qdrant-connection-refused).

---

## 5. Streamlit Cloud Frontend Deployment

### 5.1 Tạo app

1. Đăng nhập [share.streamlit.io](https://share.streamlit.io).
2. **New app** → chọn repo GitHub.
3. **Main file path**: `app_streamlit.py`.
4. **Branch**: `main` (hoặc nhánh deploy của bạn).
5. **App URL**: để tự sinh, ví dụ: `https://maia-ui.streamlit.app`.

### 5.2 Cấu hình Secrets

Streamlit Cloud dùng file `secrets.toml` (không commit lên git).

Vào **Settings** → **Secrets**, paste nội dung sau (thay giá trị thật):

```toml
# .streamlit/secrets.toml
MAIA_API_URL = "https://maia-api.onrender.com"
QDRANT_URL = "https://<cluster-id>.gcp.cloud.qdrant.io"
QDRANT_API_KEY = "<your-qdrant-api-key>"
STORAGE_DIR = "/tmp/storage"
LLAMA_INDEX_DATA_PLANE = "true"
DEFAULT_EMPLOYEE_ID = "emp_001"
BOOTSTRAP_FIRST_ADMIN = "true"
JWT_SECRET_KEY = "<same-as-render>"
CORS_ORIGINS = "https://maia-ui.streamlit.app"
API_BASE_URL = "https://maia-api.onrender.com"
APP_BASE_URL = "https://maia-ui.streamlit.app"
CLOUDFLARE_ACCOUNT_ID = "<account-id>"
CLOUDFLARE_API_TOKEN = "<api-token>"
CLOUDFLARE_MODEL = "@cf/meta/llama-3.1-8b-instruct"
```

> **Lưu ý**: Streamlit Cloud inject secrets vào `st.secrets` — MAIA đọc từ environment variables, nặc danh secrets cần map qua **Advanced settings** → **Environment variables** nếu app không auto-load `secrets.toml` vào process env. Nếu `app_streamlit.py` không đọc `st.secrets`, hãy thêm env vars thủ công trong Streamlit Cloud settings.

### 5.3 Deploy

Streamlit Cloud tự động deploy khi save. Theo dõi **Logs** để đảm bảo không có import error.

### 5.4 Verify frontend

Mở URL `https://maia-ui.streamlit.app`:
- Trang đăng nhập hiện ra.
- Đăng ký tài khoản mới → tự thành admin (vì `BOOTSTRAP_FIRST_ADMIN=true`).
- Đăng nhập thành công → vào màn hình chat.

---

## 6. First Ingestion

Sau khi cả 2 services đã chạy, cần nạp dữ liệu vào Qdrant Cloud.

### 6.1 Chạy ingestion từ local

**Cách A: Dùng API Render (khuyến nghị — có auth)**

```bash
# Lấy admin token bằng cách đăng nhập trước, hoặc dùng endpoint /ingest/enterprise
# /ingest/enterprise tự động nạp data/enterprise, không cần upload file
curl -X POST "https://<RENDER_API_URL>/ingest/enterprise" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "default"}'
```

> Lưu ý: Endpoint `/ingest` và `/ingest/enterprise` yêu cầu auth. Nếu chưa có tài khoản admin, đăng ký trước trên UI hoặc tạo user qua API.

**Cách B: CLI local (cần cài dependencies)**

```bash
# Set env vars cho Qdrant Cloud
export QDRANT_URL="https://<cluster-id>.gcp.cloud.qdrant.io"
export QDRANT_API_KEY="<api-key>"

# Nạp tài liệu mẫu
python -m maia.cli ingest

# Hoặc nạp tài liệu enterprise
python -m maia.cli ingest --enterprise
```

CLI `ingest` đọc `QDRANT_URL`, `QDRANT_API_KEY`, `DATA_DIR`, `ENTERPRISE_DATA_DIR` từ environment. Không có flag dòng lệnh cho các giá trị này.

### 6.2 Verify ingestion

```bash
python -c "from qdrant_client import QdrantClient; c=QdrantClient(url='<QDRANT_URL>', api_key='<API_KEY>'); print('Points:', c.count('maia_knowledge').count)"
```

Mở Streamlit UI → hỏi về chính sách nghỉ phép → phải có trả lời có trích dẫn `[S1]`.

---

## 7. Production Checklist

Kiểm tra toàn bộ trước khi công bố:

- [ ] **JWT_SECRET_KEY** đã set (random 64 ký tự, không dùng giá trị mặc định).
- [ ] **CORS_ORIGINS** đúng domain frontend (không để trống, không dùng `*`).
- [ ] **ENVIRONMENT=production** trên cả 2 services.
- [ ] **Cloudflare creds** hợp lệ (`/health` trả về `qdrant_status: ok` và LLM trả lời được).
- [ ] **Qdrant Cloud** cluster đã tạo + payload indexes đã tạo + dữ liệu đã ingest.
- [ ] **`/health`** trả về `status: ok` từ Render URL.
- [ ] **Login** end-to-end: đăng ký → đăng nhập → `POST /auth/login` trả `access_token`.
- [ ] **Chat** trả về câu trả lời có căn cứ (`has_evidence: true` + `citations` không rỗng).
- [ ] **Admin dashboard** (`/admin/*`) truy cập được với tài khoản admin.
- [ ] **BM25 cache** tự rebuild sau cold start (xem [Troubleshooting](#bm25-cache-rebuilding)).
- [ ] **Password reset** email flow hoạt động (nếu SMTP đã cấu hình).
- [ ] **CORS** không có lỗi trong browser console (F12 → Network).

---

## 8. Troubleshooting

### 8.1 Cold start trên Render free tier

Render free tier **spin down** sau 15 phút không có request. Lần request tiếp theo sẽ mất 10-30 giây để wake up.

**Giải pháp:**
- Dùng UptimeRobot / cron ping `/health` mỗi 10 phút để giữ service awake.
- Hoặc nâng lên plan paid ($7/tháng) để always-on.

### 8.2 BM25 cache rebuilding

`storage/bm25_cache.json` nằm trên ephemeral disk (`/tmp/storage`) → mất khi Render restart. Hệ thống tự động rebuild BM25 index khi restart, nhưng lần đầu có thể chậm.

**Giải pháp:**
- Chấp nhận rebuild tự động (miễn là `/health` đã `ok`).
- Hoặc mount persistent disk (Render paid) và set `STORAGE_DIR=/persistent/storage`.

### 8.3 CORS issues

Triệu chứng: browser console báo `Access-Control-Allow-Origin` manh môn.

**Kiểm tra:**
1. `CORS_ORIGINS` trên Render phải khớp chính xác domain Streamlit (kéo `https://`, không có trailing slash).
2. Nếu Streamlit Cloud dùng custom domain, thêm domain đó vào `CORS_ORIGINS`.
3. Sau khi sửa env var, Render tự động redeploy.

### 8.4 JWT token errors

Triệu chứng: `401 Unauthorized` ngay sau đăng nhập thành công.

**Nguyên nhân phổ biến:**
- `JWT_SECRET_KEY` thay đổi giữa các request ( ephemeral mode ). Kiểm tra `JWT_SECRET_KEY` đã set cố định.
- Render scale >1 instance (free tier thường 1 instance, nhưng nếu có 2, session DB trên local disk không sync). Giải pháp: dùng external Redis hoặc nâng plan.
- Clock skew giữa Render và Cloudflare (hiếm). Kiểm tra `ACCESS_TOKEN_EXPIRE_MINUTES=15`.

### 8.5 Qdrant connection refused

- Kiểm tra `QDRANT_URL` có `https://` không.
- Kiểm tra `QDRANT_API_KEY` đúng (copy từ Qdrant Cloud dashboard, không có khoảng trắng thừa).
- Qdrant Cloud free tier có rate limit — nếu quá tải, chờ 1-2 phút rồi retry.

### 8.6 Cloudflare LLM timeout / 401

- Kiểm tra `CLOUDFLARE_ACCOUNT_ID` đúng (UUID từ dashboard).
- Kiểm tra token có quyền `Workers AI: Edit`.
- Free tier có giới hạn requests/ngày — nếu hết, LLM trả 429. MAIA sẽ tự động fallback sang templated answer khi không có Cloudflare creds.

### 8.7 Streamlit không kết nối được API

Triệu chứng: Streamlit báo `Không kết nối được API`.

**Kiểm tra:**
1. `MAIA_API_URL` trong Streamlit secrets/env đúng domain Render backend.
2. Backend `/health` trả 200 từ browser (không phải curl).
3. Render backend không bị sleep (ping `/health` trước).
4. CORS đã bao gồm domain Streamlit.

---

## Appendix: Local Docker (Alternative)

Nếu không dùng Render/Streamlit Cloud, có thể deploy toàn bộ bằng Docker:

```bash
# Build
docker build -t maia:latest .

# Run API
docker run -d \
  -e SERVICE=api \
  -e QDRANT_URL=http://host.docker.internal:6333 \
  -e CLOUDFLARE_ACCOUNT_ID=... \
  -e CLOUDFLARE_API_TOKEN=... \
  -e JWT_SECRET_KEY=... \
  -p 8000:8000 \
  -v $(pwd)/storage:/tmp/storage \
  maia:latest

# Run UI
docker run -d \
  -e SERVICE=ui \
  -e MAIA_API_URL=http://localhost:8000 \
  -p 8501:8501 \
  maia:latest
```

Hoặc dùng docker-compose có sẵn trong repo:

```bash
docker compose -f deploy/docker/compose.infra.yml \
              -f deploy/docker/compose.app.yml \
              -f deploy/docker/compose.ui.yml \
              up -d
```

---

## Appendix: Render Blueprint Reference

File `render.yaml` trong repo định nghĩa sẵn cả 2 services. Khi push lên GitHub và connect với Render, các env var `sync: false` cần được điền thủ công trong Render Dashboard lần đầu.

Sau lần đầu, Render sẽ giữ nguyên cấu hình và auto-deploy khi có push mới lên nhánh `main`.
