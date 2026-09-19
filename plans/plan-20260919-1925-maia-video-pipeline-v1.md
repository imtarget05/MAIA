# MAIA Video Pipeline — Kế hoạch tích hợp Encode/Decode/Transcode (v1)

> **Status:** Implemented & verified (V1) — 2026-09-19.
> **Branch:** `codex/maia-video-pipeline-v1`. **CI:** ruff ✅ · pyright ✅ · `pytest tests/` 377 passed/10 skipped ✅.
> **Integrated evidence:** `scripts/video_e2e_integrated.py` → INTEGRATION PASS on real PostgreSQL + MinIO + Alembic
> (`maia-video-postgres:5434`, `maia-video-minio:9000`) + real ffmpeg (libx264 → 480p .mp4, ffprobe metadata,
> presigned download URL).
> **Target repo:** imtarget05/MAIA (phần S1–S4) **và imtarget05/Smart-Document-Chatbot** (phần S5–S6). Nhánh dự kiến: `codex/maia-video-pipeline-v1`, `codex/sdc-video-worker-v1`.
> **Đối chiếu với:** kế hoạch 4 giai đoạn "Video Encoding/Decoding/Transcoding" + `docs/PROJECT_2_STREAMING.md` + `plans/plan-20260919-1820-maia-service-desk-v1.md` + SDC `docs/adr/0004-durable-ingestion-job-queue.md`.

**Goal:** Bổ sung pipeline xử lý video (nhận job → lưu file nguồn → decode/transcode → xuất MP4/HLS) vào hệ thống MAIA sẵn có, tái sử dụng tối đa hạ tầng hiện hữu thay vì xây mới từ đầu.

---

## S1. Kết quả khảo sát hiện trạng (Giai đoạn 1 — System Audit)

### 1.1 Hạ tầng phần cứng (đã kiểm tra trực tiếp trên máy dev)

| Hạng mục | Kết quả thực tế | Ảnh hưởng đến kế hoạch |
|---|---|---|
| CPU/GPU | **Apple M1 Pro**, 8 cores, 16 GB RAM, Metal 4 | **Không có NVIDIA** → NVENC/NVDEC + CUDA **không áp dụng** cho máy này. Intel QuickSync / AMD AMF cũng không có. |
| HW encoder khả dụng | **VideoToolbox** (`h264_videotoolbox`, `hevc_videotoolbox`, `prores_videotoolbox`) | Hardware acceleration trên macOS là VideoToolbox, không phải NVENC. Kế hoạch gốc cần hiệu chỉnh. |
| FFmpeg | Đã cài sẵn (`/opt/homebrew/bin/ffmpeg`) với `libx264`, `libx265` + VideoToolbox | Không cần build lại. |
| Docker | Docker Desktop 29.6.1 | ⚠️ **Caveat:** Docker trên macOS chạy Linux VM → **VideoToolbox không dùng được trong container**. Worker container chỉ có CPU (libx264/x265); NVENC chỉ khả dụng khi deploy lên server Linux có GPU NVIDIA. |
| Disk / I/O | APFS (NVMe), còn trống ~303 GB | I/O ổn cho temp caching — đáp ứng yêu cầu SSD/NVMe làm buffer. |
| RAM | 16 GB | Vài worker FFmpeg song song là vừa đủ cho dev/thesis; production cần server riêng. |

**Kết luận S1.1:** Kế hoạch gốc giả định "NVIDIA CUDA + NVCODEC" là mặc định — cần thay bằng **`encoder_profile` trừu tượng theo nền tảng**: `videotoolbox` (macOS host) / `nvenc` (Linux + NVIDIA) / `libx264` (CPU fallback, chạy được mọi nơi kể cả container). Profile chọn qua config, không hard-code.

### 1.2 Kiến trúc phần mềm sẵn có (đọc mã nguồn, khóa theo repo ngày 2026-09-19)

| Thành phần trong kế hoạch | Hiện trạng MAIA | Bằng chứng |
|---|---|---|
| API Gateway / RESTful | ✅ FastAPI 40+ endpoints, JWT + bcrypt + Google OAuth, RBAC, multi-tenant | `src/maia/api.py`, `src/maia/auth.py` |
| Queue / Event Bus | ⚠️ **Tồn tại nhưng đã bị ARCHIVED** — pipeline Kafka (KRaft, DLQ, consumer group, InMemoryBroker offline, metrics, Grafana dashboard) nằm ở `_archive/src/maia/stream/`, kèm `tests/test_no_stream_imports.py` **cấm import runtime** | `_archive/src/maia/stream/` |
| Worker pattern | ⚠️ Pattern hiện hành là **DB-backed job queue** (lease 60s + heartbeat 15s, poll loop) theo hướng Service Desk — không dùng broker | `src/maia/servicedesk/settings.py`, `src/maia/outbox_worker.py` |
| Database metadata | ⚠️ Legacy core dùng **SQLite WAL**; Service Desk (convention mới nhất) **bắt buộc PostgreSQL** — URL non-Postgres bị từ chối outright | `src/maia/servicedesk/db.py::make_engine` |
| Object Storage | ❌ **Chưa có** MinIO/S3 — chỉ có local `STORAGE_DIR` (volume `/app/storage`) | `config.py::STORAGE_DIR`, `deploy/docker/compose.app.yml` |
| Notifications/Webhook | ✅ Email qua SMTP với fallback outbox.json, never-raise | `src/maia/notifier.py` |
| Monitoring | ✅ Có pattern — Prometheus metrics + consumer lag + `grafana/maia-dashboard.json` (trong archive); pipeline tracer luôn bật | `_archive/src/maia/stream/metrics.py`, `src/maia/observability.py` |
| Docker Compose | ✅ Tách lớp: `compose.infra/app/stream/ui.yml` | `deploy/docker/` |
| Code video hiện có | ❌ Không có — `grep -ri video src/` rỗng | — |
### 1.3 Khoảng trống phải lấp (gap list)

1. **Queue:** (A) "hồi sinh" Kafka từ `_archive/` thành module mới (không import trực tiếp — vi phạm test cấm), hoặc (B) DB-backed job queue trên PostgreSQL theo mô hình Service Desk. → **Khuyến nghị V1: (B)** — Kafka đã bị archive một cách chủ đích; DB-queue ít hạ tầng hơn, đủ cho quy mô thesis. Kafka chỉ cân nhắc khi cần hàng nghìn job/giờ.
2. **Storage:** thêm **MinIO** service vào compose (S3-compatible, Pre-signed URL đúng yêu cầu Giai đoạn 4). V1 offline mode fallback local `STORAGE_DIR`.
3. **Metadata DB:** schema riêng `vid_` trên PostgreSQL (không đụng bảng `sd_` của Service Desk, không đụng bảng legacy), migration Alembic riêng.
4. **Video worker:** module mới `src/maia/video/` — chưa tồn tại gì.
5. **FFmpeg trong container:** image phải có ffmpeg; deploy Linux GPU thì dùng image có `--enable-nvenc`.

---

## S2. Thiết kế kiến trúc & Tech Stack (Giai đoạn 2 — hiệu chỉnh theo hiện trạng)

| Thành phần | Kế hoạch gốc | **Điều chỉnh cho MAIA** |
|---|---|---|
| Video Engine | FFmpeg, GStreamer, OpenCV | **FFmpeg subprocess** (đã cài sẵn). GStreamer/OpenCV không cần cho V1. |
| HW gia tốc | NVIDIA CUDA + NVCODEC | `EncoderProfile`: `videotoolbox` / `nvenc` / `libx264` (CPU fallback). Chọn qua `VID_ENCODER_PROFILE`. |
| Queue | Kafka / RabbitMQ | **PostgreSQL DB-queue** (`vid_jobs`, lease/heartbeat). Kafka giữ làm phase 2 khi cần throughput. |
| Object Storage | MinIO / S3 | **MinIO trong `compose.video.yml`** (bucket `maia-video-in` / `maia-video-out`, Pre-signed URL). Offline: local `STORAGE_DIR`. |
| Streaming Protocol | HLS, DASH, RTSP | V1: **MP4 + HLS VOD** (`-f hls -hls_time 6`). RTSP/RTMP live là scope sau. |
| Metadata DB | PostgreSQL/MySQL | **PostgreSQL** — schema `vid_`, Alembic migration riêng (nhất quán convention S4). |
| Notification/Webhook | Telegram/Zalo/Email | **Tái dùng `notifier.send_email`** (SMTP + outbox fallback) + HTTP webhook HMAC-signed khi hoàn thành. |

### Sơ đồ pipeline (V1)

```
[User / API client]
      │ POST /video/jobs (upload file hoặc URL nguồn, preset output)
      ▼
[MAIA API] ── upload ──► [MinIO: maia-video-in]
      │ ghi hàng vid_jobs (status=queued, idempotency key)
      ▼
[PostgreSQL vid_jobs] ◄── lease/heartbeat ──► [Video Worker ×N]
                                                 │ (poll queued job, lease 60s)
                                                 ├─ fetch source (presigned)
                                                 ├─ FFmpeg decode/transcode
                                                 │   (profile: videotoolbox|nvenc|libx264)
                                                 ├─ upload MP4/HLS chunks → [MinIO: maia-video-out]
                                                 ├─ update vid_jobs (status=done, metadata)
                                                 └─ webhook + notify_email (re-use notifier)
                                                 ▼
                              [Prometheus metrics + Grafana] [Retention cleanup]
```

---

## S3. Lộ trình triển khai (Giai đoạn 3 — Roadmap, task-level)

### T1 — Khung module `src/maia/video/` (tương tự cấu trúc `servicedesk/`)

- `settings.py`: prefix `VID_` (`VID_MODE`, `VID_DATABASE_URL`, `VID_ENCODER_PROFILE`, `VID_STORAGE_BACKEND=minio|local`, `VID_MINIO_*`, `VID_WEBHOOK_URL`, `VID_WEBHOOK_SECRET_REF`, `VID_RETENTION_DAYS`, `VID_WORKER_CONCURRENCY`, `VID_LEASE_SECONDS`).
- `db.py`: engine PostgreSQL (bắt buộc khi `VID_MODE=integrated`), schema `vid_` riêng.
- `models.py`: bảng `vid_jobs` — `job_id (uuid pk)`, `tenant_id`, `status (queued|leased|done|failed)`, `source_uri`, `preset (json)`, `output_uri`, `error`, `attempt`, `leased_by`, `lease_expires_at`, `heartbeat_at`, `created_at`, `finished_at`.
- `schemas.py`: Pydantic `VideoJobCreate` / `VideoJobEvent` (idempotency key = hash(source_sha256 + preset)).
- Alembic migration riêng (`alembic_video`).

### T2 — FFmpeg pipeline (`pipeline.py` + `encoder.py`)

- `encoder.py`: probe nền tảng (có `h264_videotoolbox`? `h264_nvenc`?) → sinh argument ffmpeg; libx264 là fallback bắt buộc.
- `pipeline.py`: dựng lệnh FFmpeg cho từng job type: `transcode` (ladder 1080p→720p/480p, bitrate), `remux`, `extract_audio`, `hls_vod`; `ffprobe` metadata (duration, resolution, codec) ghi vào `vid_jobs`.
- Input Handler: nhận file upload (multipart) hoặc URL → đẩy lên MinIO/local trước khi enqueue; **không** nhận RTSP trong V1.

### T3 — Worker (`worker.py`)

- Poll loop: `lease` job (`UPDATE ... WHERE status='queued' ... FOR UPDATE SKIP LOCKED`) → heartbeat → chạy FFmpeg subprocess (timeout + kill process-tree) → upload output → commit trạng thái; lease expire thì job khác lấy lại (at-least-once + idempotent output theo `job_id` → effectively-once).
- Fail: retry theo `attempt` (mặc định 3), quá hạn → `status=failed` + bảng `vid_jobs_failed` (pattern DLQ của archive).
- Completion: `notifier.send_email` + HTTP webhook POST (HMAC-signed) tới `VID_WEBHOOK_URL` nếu cấu hình.

### T4 — API endpoints (router riêng `video.api`, mount vào app chính)

- `POST /video/jobs` — tạo job (auth JWT, `tenant_id` từ token).
- `GET /video/jobs/{job_id}` — trạng thái + metadata.
- `GET /video/jobs/{job_id}/download` — short-lived token (JWT TTL 5 phút) khi backend local; Pre-signed URL MinIO khi backend minio.
- `DELETE /video/jobs/{job_id}` — xoá job + output (đi qua retention).

### T5 — Docker Compose (`deploy/docker/compose.video.yml`)

- `postgres`, `minio` + console, `video-worker` (image base có ffmpeg; GPU image chỉ khi deploy server NVIDIA) — **opt-in** như `compose.stream.yml`, không chạy mặc định cùng full stack.

### T6 — Tests offline (`tests/test_video.py`, pattern theo `tests/test_stream.py`)

- Fake FFmpeg (stub subprocess) cho unit test pipeline/encoder/worker; chạy được offline không cần MinIO/Postgres thật (mode `offline` dùng SQLite-in-memory **chỉ cho test** — production vẫn Postgres-bắt-buộc, đúng ràng buộc "no silent fallback").
- Idempotency test: re-lease cùng job không tạo output trùng.

---

## S4. Vận hành, Giám sát & Bảo mật (Giai đoạn 4)

### 4.1 Monitoring

- Custom Prometheus metrics trong `src/maia/video/metrics.py` (pattern `_archive/stream/metrics.py`):
  - `maia_video_jobs_queued` (queue depth — feed vào auto-scaling),
  - `maia_video_jobs_done_total / failed_total`,
  - `maia_video_encode_seconds` (histogram, label codec/profile),
  - `maia_video_processed_source_seconds_total` (tổng giây video đầu vào đã xử lý — quy về "processing time per video-minute"),
  - `maia_video_worker_active`.
- ⚠️ **GPU metrics (GPU Utilization/VRAM) không có trên máy dev M1** — chỉ khả dụng khi deploy lên server NVIDIA (thêm DCGM exporter vào compose lúc đó). Trên máy dev: theo dõi CPU/RAM/temperature host + encode fps của ffmpeg.
- Grafana: mở rộng `grafana/maia-dashboard.json` thêm panel video.

### 4.2 Scaling

- Worker tách process khỏi API (đúng ràng buộc "worker không nằm trong web process" của Service Desk plan).
- Scale = tăng replicas `video-worker`; DB-queue lease tự phân phối (mỗi job 1 worker nhờ `SKIP LOCKED`); auto-scaling theo `maia_video_jobs_queued` (HPA/K8s phase sau).

### 4.3 Bảo mật & Retention

- Download chỉ qua **token TTL ngắn** hoặc **MinIO Pre-signed URL**; bucket không public.
- Job scoped theo `tenant_id` (nhất quán multi-tenant của MAIA).
- **Retention policy:** dọn theo `VID_RETENTION_DAYS` (mặc định 7 ngày) — xoá output quá hạn + temp files (`<tmpdir>/vid_<job_id>/*`) ngay sau upload thành công; vòng dọn chạy trong worker loop mỗi giờ.

---

## Global Constraints (ràng buộc — nhất quán với repo policy)

- Không import trực tiếp từ `_archive/` (vi phạm `tests/test_no_stream_imports.py`) — chỉ tham khảo pattern; code video nằm ở module mới `src/maia/video/`.
- Production/integrated mode: **PostgreSQL bắt buộc**, không fallback SQLite im lặng (convention S4 của Service Desk).
- Worker là process riêng, heartbeat liên tục; không chạy FFmpeg trong web process.
- FFmpeg subprocess phải có timeout + kill process-tree; job lỗi không bao giờ được báo "done".
- Output path/idempotency key dẫn xuất từ `job_id`; re-run không tạo file trùng.
- Không tuyên bố exactly-once cho webhook HTTP; failure phải visible qua metric + bảng failed.
- Secrets qua tham chiếu (env/secret store), không inline trong code/compose.

## Definition of Done (V1)

| # | DoD | Trạng thái |
|---|---|---|
| 1 | `POST /video/jobs` nhận upload + preset, trả job_id | ✅ `POST /video/jobs` (multipart, idempotent, 202) |
| 2 | Worker transcode MP4 1080p→720p/480p + HLS, upload MinIO/local | ✅ `VideoWorker.step` (transcode/hls_vod/remux/extract_audio) |
| 3 | Metadata (duration/resolution/codec/status) vào Postgres `vid_` | ✅ ffprobe → `metadata_json` (JSONB/JSON variant) |
| 4 | Encoder profile videotoolbox/nvenc/libx264 tự chọn + fallback | ✅ `encoder.resolve_encoder` (auto probe, HW→CPU fallback) |
| 5 | Download qua token TTL / presigned URL | ✅ Presigned MinIO + authenticated FileResponse (local) |
| 6 | Webhook + email khi hoàn thành (re-use notifier) | ✅ HMAC webhook + notifier injection (never-raise) |
| 7 | Metrics Prometheus + Grafana panel video | ✅ `metrics.py` + `GET /video/metrics` + `grafana/maia-video-dashboard.json` |
| 8 | Retention dọn temp + output quá hạn | ✅ `retention.run_retention` + sweep giờ trong worker loop |
| 9 | Compose opt-in `compose.video.yml` | ✅ + `Dockerfile.video` + `requirements-video.txt` |
| 10 | Tests offline (`pytest tests/test_video.py`) xanh | ✅ 34/34 pass (kèm real-ffmpeg e2e, ruff sạch) |


---

## S5. Mở rộng phạm vi: tích hợp vào Smart-Document-Chatbot

> Audit riêng cho repo `Smart-Document-Chatbot` (đọc mã nguồn + ADR + compose ngày 2026-09-19).

### 5.1 Hiện trạng Smart-Document-Chatbot

| Thành phần trong kế hoạch | Hiện trạng SDC | Bằng chứng |
|---|---|---|
| Kiến trúc | Polyglot: **Spring Boot 3.2 (Java 17) backend :8080** + **FastAPI agent service (Python 3.11) :9000** + LLM Router :8001 + React 18 SPA; Airflow, Keycloak SSO | `docker/docker-compose.yml`, `backend/pom.xml`, `agent/` |
| Database | ✅ **Neon PostgreSQL** (managed) sẵn — không cần thêm MinIO-metadata hay DB mới | `SPRING_DATASOURCE_URL: ${NEON_JDBC_URL}` |
| Object Storage | ✅ **Cloudflare R2 (S3-compatible, 10GB free)** — chưa thấy generate Pre-signed URL trong backend (grep `presign` rỗng) → cần bổ sung khi làm video output | `STORAGE_PROVIDER=r2`, `R2_*` env trong compose |
| Queue / Job | ✅ **ADR-004: Durable DB-backed job queue** — `document_ingestion_jobs` (Flyway V16): idempotent enqueue (partial unique index), `SELECT ... FOR UPDATE SKIP LOCKED`, lease-timeout crash recovery, retry exponential backoff 30s→2m→8m, trạng thái DEAD replayable qua `POST /admin/ingestion-jobs/{id}/replay` | `docs/adr/0004-durable-ingestion-job-queue.md` |
| Broker | ❌ **Chủ đích KHÔNG dùng Kafka/RabbitMQ** — ADR-004 kết luận broker là "cargo-cult engineering" ở quy mô hiện tại; migration trigger: nhiều worker type, fan-out, >10 jobs/s | ADR-004, "Consequences" |
| Monitoring | ✅ Loki + Promtail + Langfuse (ClickHouse); không thấy Prometheus/Grafana trong `docker-compose.monitoring.yml` | `docker/docker-compose.monitoring.yml` |
| Code video hiện có | ❌ Không có | — |

### 5.2 Thiết kế cho SDC — tái dùng pattern ADR-004

**Nguyên tắc: đi đúng con đường ADR-004 đã chọn (DB-queue, no broker), không áp đặt Kafka từ kế hoạch gốc.**

- **Bảng job:** Flyway migration mới (`V17__video_jobs.sql` hoặc đánh số tiếp theo) tạo `video_jobs` theo đúng pattern `document_ingestion_jobs`: partial unique index idempotent enqueue `(source_hash, job_type)`, `PENDING/RUNNING/DONE/DEAD`, lease timeout, exponential backoff, DEAD replayable.
- **Worker:** service riêng `video-worker` (Python 3.11 + ffmpeg image) poll bảng `video_jobs` trên cùng Neon Postgres bằng `FOR UPDATE SKIP LOCKED` — **không chạy FFmpeg trong web process** (khác ADR-004 chỉ vì workload CPU nặng + dài, cần process tách biệt và kill được process-tree).
- **API:** REST endpoints phía Spring Boot: `POST /api/video/jobs`, `GET /api/video/jobs/{id}`, `GET /api/video/jobs/{id}/download`, `POST /admin/video-jobs/{id}/replay` (đối xứng với admin ingestion replay). JWT + CSRF + rate-limit stack có sẵn áp dụng nguyên si.
- **Storage:** dùng **R2 luôn** (S3-compatible) thay vì MinIO: bucket `smart-doc-video-in` / `smart-doc-video-out`; bổ sung SDK call sinh **Pre-signed URL** cho source upload (browser → R2 trực tiếp) và output download — vá đúng khoảng trống grep ở trên.
- **Encoder profile:** giống S1.1 — `libx264` mặc định trong container Linux; `videotoolbox` chỉ khi worker chạy host macOS; `nvenc` khi deploy GPU server.
- **Tích hợp ngược vào RAG (giá trị cộng thêm):** sau khi transcode xong, có thể enqueue job type `video_transcript` (extract audio → STT) để nạp nội dung video vào knowledge base — optional, phase sau.



---

## S6. So sánh & tái sử dụng chung giữa hai hệ thống

| Khía cạnh | MAIA | Smart-Document-Chatbot |
|---|---|---|
| Queue | DB-queue mới `vid_jobs` (khuyến nghị V1); Kafka chỉ phase 2 | DB-queue `video_jobs` theo ADR-004 (no broker là quyết định đã chốt) |
| Storage | MinIO (self-host) hoặc local `STORAGE_DIR` | **Cloudflare R2** (đã có, chỉ bổ sung Pre-signed URL) |
| Metadata DB | PostgreSQL schema `vid_` (Alembic) | PostgreSQL bảng `video_jobs` (Flyway) trên Neon |
| API host | FastAPI (`src/maia/api.py` + router video) | Spring Boot `/api/video/*` + admin replay |
| Worker | `src/maia/video/worker.py` (Python) | `video-worker` service riêng (Python, poll Neon) |
| FFmpeg/Encoder | Dùng chung thiết kế `encoder_profile` (videotoolbox/nvenc/libx264) | Như MAIA |
| Notification | `notifier.send_email` + webhook HMAC | Email/notification có sẵn + webhook |
| Monitoring | Prometheus + Grafana (mở rộng dashboard) | Loki/Langfuse; Prometheus bổ sung nếu cần metrics số |

**Tái sử dụng mã nguồn:** core FFmpeg logic (builder argument, encoder probe, ffprobe metadata, idempotency key, retention cleanup) nên viết thành **package Python thuần** dùng chung được bởi cả `maia.video` (MAIA) và `video-worker` (SDC) — chỉ khác lớp queue/storage/API phía ngoài. Điều này tránh viết 2 lần phần dễ phát sinh bug nhất (FFmpeg argument + timeout/kill process-tree).

**Thứ tự triển khai đề xuất:** MAIA trước (worker + API + compose, T1–T6) → chiết tách core thành package chung → SDC (Flyway migration + worker container + Spring endpoints + R2 presigned).

## Cập nhật DoD phần SDC (bổ sung)

| # | DoD | Trạng thái |
|---|---|---|
| 11 | Flyway migration `video_jobs` đúng pattern ADR-004 (idempotent, lease, DEAD replayable) | ☐ |
| 12 | `video-worker` container poll Neon, chạy FFmpeg cách ly web process | ☐ |
| 13 | Spring endpoints `/api/video/*` + `/admin/video-jobs/{id}/replay` (JWT/CSRF/rate-limit) | ☐ |
| 14 | R2 Pre-signed URL cho upload source + download output | ☐ |
| 15 | Test JUnit cho job service (pattern `DocumentJobServiceTest`) + test Python worker | ☐ |
