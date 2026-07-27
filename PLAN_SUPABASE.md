# Arya_Tool — Kế hoạch chuyển toàn bộ dữ liệu sang Supabase

Trạng thái: **đã chuẩn bị local; chờ project/key để push và cutover**  
Ngày cập nhật: **2026-07-28**

Tiến độ ngày 2026-07-27:

| Phase | Trạng thái |
|---|---|
| S0 config/CLI scaffold | Xong local; chưa link remote |
| S1 schema 18 bảng | Xong migration; chờ `db push` |
| S2 database adapter | Xong SQLite/Postgres adapter và unit test |
| S3 Auth/RLS | Xong SQL trigger/policy; `/stats` fail closed, Auth session chưa nối |
| S4 Storage | Xong local media + private artifact service/RLS; chưa chạy remote |
| S5 ETL | Xong local dry-run/execute/reconciliation media + artifact; chưa chạy remote |
| S6 cutover/rollback | Xong checklist generator local; chưa diễn tập/cutover remote |
| S7 vận hành | Chưa triển khai |

## 1. Mục tiêu

Chuyển nguồn dữ liệu chính của Arya_Tool từ SQLite và filesystem cục bộ sang:

- **Supabase Postgres:** toàn bộ 18 bảng nghiệp vụ/metadata.
- **Supabase Storage:** media, file import và report cần lưu lâu dài.
- **Supabase Auth:** đăng nhập dashboard và định danh owner.
- **Row Level Security (RLS):** cô lập dữ liệu theo owner.

FastAPI, agent, Telegram bot, scheduler và publish worker tiếp tục chạy bằng
Python. Cách này giữ nguyên phần lớn service/query SQLAlchemy hiện tại và tránh
viết lại toàn bộ ứng dụng sang Data API.

## 2. Ranh giới bắt buộc

| Thành phần | Đích | Lý do |
|---|---|---|
| 18 bảng SQL | Supabase Postgres | Một nguồn dữ liệu chung, có constraint và transaction |
| Media ảnh/video | Private Storage `arya-media` | Không phụ thuộc thư mục `media/` trên một máy |
| CSV import, report/eval cần lưu | Private Storage `arya-artifacts` | Quản lý tập trung, tải qua signed URL |
| User dashboard | Supabase Auth + `public.users` | Có session rõ ràng và map owner cho RLS |
| Facebook/Instagram browser profile | **Giữ local** | Đây là dữ liệu Chrome đang hoạt động, có phiên đăng nhập; không upload cookie/profile lên cloud |
| OpenRouter, Telegram, DB password, Supabase secret key | **Giữ trong `.env`/secret manager local** | Secret khởi động không được đưa vào bảng, prompt, log hoặc frontend |
| FastAPI/agent/scheduler/worker | **Giữ local ở port 8010** | Browser launch và job Python cần chạy trên máy người dùng |

“Chuyển hết sang Supabase” trong kế hoạch này nghĩa là toàn bộ dữ liệu bền vững
có thể chia sẻ được chuyển sang Postgres/Storage/Auth. Browser profile và secret
runtime không phải dữ liệu phù hợp để đồng bộ cloud.

## 3. Kiến trúc đích

```text
Browser localhost:8010
        |
        v
Arya_Tool FastAPI
  |-- SQLAlchemy + psycopg ------> Supabase Postgres
  |-- Supabase Storage HTTP API -> private Storage
  |-- Scheduler/publish worker --> Postgres job queue
  |-- Chrome profile -----------> ~/.arya-tool/... (local only)
  |-- OpenRouter/Telegram ------> secret từ env local
```

Quyết định kết nối:

- Runtime FastAPI dùng **direct connection** nếu máy có IPv6; nếu mạng chỉ có
  IPv4 thì dùng **Supavisor session mode port 5432**.
- Migration, dump và kiểm tra schema dùng direct connection.
- Không dùng transaction pooler port 6543 cho process local chạy lâu, trừ khi
  đã kiểm tra lại toàn bộ transaction/session behavior.
- SQLite vẫn được giữ cho unit test offline và làm đường rollback trong giai
  đoạn chuyển đổi.

## 4. Inventory cần chuyển

Nhóm agent core:

- `users`, `conversations`, `messages`
- `tasks`, `steps`, `llm_calls`
- `notes`, `todos`, `scheduled_jobs`

Nhóm social affiliate:

- `social_accounts`, `media_assets`, `affiliate_products`
- `social_posts`, `content_generations`
- `publish_jobs`, `publish_attempts`
- `affiliate_events`
- `artifacts`

Thứ tự import phải theo foreign key:

1. `users`
2. `conversations`, `notes`, `todos`, `scheduled_jobs`,
   `social_accounts`, `media_assets`, `affiliate_products`, `artifacts`
3. `tasks`, `messages`, `social_posts`
4. `steps`, `llm_calls`, `content_generations`, `publish_jobs`
5. `publish_attempts`, `affiliate_events`

## 5. Phase S0 — Project và môi trường

Deliverables:

- Tạo Supabase project development ở region gần Việt Nam.
- Cài Supabase CLI và chạy `supabase init`.
- Commit `supabase/config.toml`, `supabase/migrations/` và
  `supabase/seed.sql` chỉ chứa dữ liệu giả.
- Ignore `.temp`, `.branches` và mọi file secret.
- Thêm cấu hình:

```env
LAPLACE_DB_URL=postgresql+psycopg://...
LAPLACE_SUPABASE_URL=https://<project-ref>.supabase.co
LAPLACE_SUPABASE_SECRET_KEY=
LAPLACE_SUPABASE_MEDIA_BUCKET=arya-media
LAPLACE_SUPABASE_ARTIFACT_BUCKET=arya-artifacts
```

Gate:

- Không key/password nào được commit.
- App kết nối được bằng SSL.
- `SELECT 1` và health check database pass.

## 6. Phase S1 — Baseline schema PostgreSQL

Tạo một SQL migration chuẩn trong `supabase/migrations/`; đây là nguồn sự thật
duy nhất cho schema remote. Không sửa schema production trực tiếp trong Table
Editor.

Chuyển kiểu dữ liệu:

- `JSON` → `JSONB`.
- Timestamp → `TIMESTAMPTZ`, luôn ghi UTC.
- Tiền hoa hồng/cost → `NUMERIC`, không tiếp tục dùng floating point cho tiền.
- ID hiện tại vẫn là integer identity để giảm phạm vi rewrite.
- Giữ toàn bộ unique/check constraint và index đang có.
- Bổ sung index cho mọi foreign key và các query dashboard/scheduler.

Quy ước migration:

- Mỗi thay đổi schema có một file timestamp riêng.
- Chạy `supabase db reset` trên local stack.
- Chạy `supabase db push --dry-run` trước mọi remote push.
- Chỉ một người/process được push migration vào một môi trường tại một thời điểm.

Gate:

- Schema sạch dựng lại được từ zero.
- SQLAlchemy metadata và Postgres schema không lệch cột/nullable/constraint.
- Test CRUD, rollback transaction, concurrent job claim và idempotency pass trên Postgres.

## 7. Phase S2 — Database adapter

Thay đổi code:

- Thêm `psycopg` cho SQLAlchemy.
- `laplace/db.py` hỗ trợ hai backend:
  - SQLite: WAL/busy timeout như hiện tại.
  - PostgreSQL: `pool_pre_ping`, giới hạn pool, connect timeout và SSL.
- Production không gọi `Base.metadata.create_all()`. Startup chỉ kiểm tra
  migration revision/schema health và fail closed nếu schema thiếu.
- Test/eval offline tiếp tục dùng SQLite.
- Thêm integration test PostgreSQL riêng, không thay unit test nhanh.

Gate:

- Đổi duy nhất `LAPLACE_DB_URL` là app chạy được trên Supabase.
- Restart scheduler không tạo duplicate publish job.
- Mất mạng/timeout không làm job bị đánh dấu thành công giả.

## 8. Phase S3 — Supabase Auth và RLS

Không đổi toàn bộ primary key sang UUID ngay. Thêm:

```text
public.users.auth_user_id UUID UNIQUE NULL
  -> auth.users(id) ON DELETE RESTRICT
```

Lộ trình:

1. Tạo Supabase Auth user cho owner dashboard.
2. Map owner local hiện tại sang `users.auth_user_id`.
3. Thay owner `#1` ẩn bằng session Supabase Auth.
4. Telegram user có thể để `auth_user_id` nullable cho đến khi được link thủ công.
5. Backend xác minh session, resolve `auth.uid()` → `public.users.id`.

RLS:

- Bật RLS cho toàn bộ bảng trong `public`.
- Owner chỉ được thao tác row có `user_id` của chính mình.
- Bảng con không có `user_id` trực tiếp kiểm tra ownership qua bảng cha.
- `anon` mặc định không có quyền đọc/ghi dữ liệu nghiệp vụ.
- Service/secret key chỉ tồn tại server-side; không render vào HTML/JavaScript.
- Thêm test phủ định: user A không đọc/sửa account, post, media, job và event của user B.

Gate:

- Truy cập không session bị 401/403.
- Cross-owner access luôn bị chặn ở cả API và database policy.
- Facebook/Instagram `auth_ref` vẫn chỉ là opaque reference; không có raw cookie.

## 9. Phase S4 — Supabase Storage

Tạo hai private bucket:

- `arya-media`: JPEG, PNG, WebP, MP4 đã validate.
- `arya-artifacts`: CSV import, report và file export.

Mở rộng `media_assets`:

```text
storage_backend  TEXT NOT NULL DEFAULT 'local'
storage_bucket   TEXT NULL
storage_key      TEXT NULL
local_path       TEXT NOT NULL  # path local hoặc URI supabase:// tương thích
```

Object key:

```text
users/<user_id>/<sha256-prefix>/<sha256>-<safe-filename>
```

Luồng upload:

1. Nhận file vào temp local.
2. Kiểm MIME, size, extension và hash như hiện tại.
3. Upload private bucket.
4. Ghi row DB với compensation nếu một bước thất bại.
5. Xóa temp file.

Luồng đọc:

- Dashboard preview dùng signed URL thời hạn ngắn.
- Publisher tải object về temp path ngay trước khi publish rồi xóa.
- Database chỉ lưu bucket/key; không lưu signed URL vì URL có hạn.
- Job cleanup tìm object mồ côi và row trỏ tới object không tồn tại.

Gate:

- Không object nào public.
- User A không lấy signed URL object user B.
- Upload retry không sinh object trùng nhờ SHA-256/idempotency.
- Xóa row/object có audit và xử lý partial failure.

## 10. Phase S5 — ETL SQLite → Supabase

Viết command riêng:

```bash
python -m laplace.migrations.sqlite_to_supabase \
  --source sqlite:///./arya-tool.db \
  --target-env LAPLACE_DB_URL \
  --dry-run \
  --report reports/supabase-dry-run.json
```

Yêu cầu:

- Không log DB URL, password, `auth_ref` hoặc nội dung nhạy cảm.
- Đọc SQLite và ghi batch/transaction đúng thứ tự foreign key.
- Giữ nguyên integer ID hiện tại.
- Reset Postgres identity sequence về `max(id) + 1`.
- Upload media sang Storage và chỉ cập nhật row sau khi upload thành công.
- Chạy lại không tạo row/object trùng: row conflict theo ID, object theo SHA-256.

Đối soát:

- Row count từng bảng và kiểm orphan foreign key.
- Tổng view/click/commission và currency.
- Số draft/approved/scheduled/published/failed.
- Hash media và số object Storage.
- Lấy mẫu timestamp UTC và JSONB.

Gate:

- Dry-run không ghi dữ liệu.
- Hai lần chạy cho cùng source cho kết quả idempotent.
- Có báo cáo reconciliation JSON và Markdown (cùng basename).

## 11. Phase S6 — Cutover và rollback

Cutover:

1. Tạo backup SQLite và copy thư mục media.
2. Dừng write API, Telegram mutation và scheduler.
3. Chạy ETL cuối.
4. Chạy reconciliation.
5. Chuyển `LAPLACE_DB_URL` và Storage backend.
6. Restart app trên port 8010.
7. Smoke test Auth, Content Studio, schedule, worker mock, analytics và
   Facebook/Instagram browser profile.
8. Mở lại write traffic.

Rollback:

- Không xóa SQLite/media cũ trong thời gian ổn định.
- Nếu gate lỗi, dừng write, đổi lại `LAPLACE_DB_URL` và restart.
- Dữ liệu phát sinh trên Supabase sau cutover phải được export/reconcile trước
  khi rollback để tránh mất event.
- Không chạy `supabase db reset --linked` trên production.

Gate:

- Có checklist người thực hiện, timestamp và kết quả từng bước.
- Rollback được diễn tập trên staging trước production.

## 12. Phase S7 — Vận hành

- Health check DB/Storage riêng; không trả secret trong lỗi.
- Alert cho DB unavailable, Storage failure, scheduler lag và job retry tăng.
- Logical backup định kỳ bằng `supabase db dump`.
- Backup object Storage riêng; database backup chỉ bảo vệ metadata, không khôi
  phục file Storage đã bị xóa.
- Kiểm quota DB, Storage và egress.
- Free project có thể bị pause khi ít hoạt động; production ổn định nên cân
  nhắc Pro.
- Rotation định kỳ DB password và Supabase secret key.

## 13. Thứ tự triển khai

1. S0 project/env
2. S1 schema migration
3. S2 adapter + PostgreSQL integration tests
4. S3 Auth/RLS
5. S4 Storage
6. S5 dry-run ETL trên staging
7. S6 cutover
8. S7 monitoring/backup

Không làm Storage/Auth trước khi Postgres schema và migration workflow ổn định.
Không xóa SQLite cũ trước khi cutover và rollback drill đều pass.

## 14. Definition of Done

- 18/18 bảng dùng Supabase Postgres làm source of truth.
- Media/import/report bền vững nằm trong private Supabase Storage.
- Dashboard dùng Supabase Auth; RLS chặn cross-owner.
- Unit test SQLite và integration test PostgreSQL đều pass.
- MockPublisher và scheduler không đăng trùng sau retry/restart.
- Reconciliation SQLite → Supabase không lệch dữ liệu.
- Browser profile Facebook/Instagram vẫn local và không lộ cookie.
- Không secret nào xuất hiện trong Git, HTML, API response, trace hoặc log.
- Có backup, cutover checklist và rollback đã diễn tập.

## 15. Tài liệu Supabase dùng làm chuẩn

- [Kết nối Postgres](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Database migrations](https://supabase.com/docs/guides/deployment/database-migrations)
- [Local development workflow](https://supabase.com/docs/guides/local-development/cli-workflows)
- [Row Level Security](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [Supabase Auth](https://supabase.com/docs/guides/auth)
- [User data và `auth.users`](https://supabase.com/docs/guides/auth/managing-user-data)
- [Private Storage buckets](https://supabase.com/docs/guides/storage/buckets/fundamentals)
- [Signed URLs](https://supabase.com/docs/reference/python/storage-from-createsignedurl)
- [Database backups](https://supabase.com/docs/guides/platform/backups)
