# Arya_Tool — Implementation Phases

Kế hoạch chuyển toàn bộ dữ liệu bền vững sang Supabase:
[PLAN_SUPABASE.md](PLAN_SUPABASE.md).

## Trạng thái triển khai

- ✅ Phase 0–2: fork, social domain, MockPublisher, worker, idempotency,
  concurrency/recovery, cooldown và daily limit.
- ✅ Phase 3 core: ba agent tool đã đăng ký và dùng confirm predicate.
- 🟡 Phase 4: dashboard đọc dữ liệu thật và REST vertical flow đã có; form web
  ghi dữ liệu/CSRF chưa làm.
- ✅ Content Studio: 6 preset văn phong, copywriter OpenRouter Free tách khỏi
  agent chính, kiểm tra deterministic và approval gate đã hoàn thành.
- ✅ Facebook/Instagram connect: browser profile tách biệt, user tự đăng nhập,
  owner isolation và không nhập/đọc cookie.
- 🟡 Phase 5: sáu lệnh Telegram và owner-only confirm pause/resume đã có;
  notification/daily summary tự động chưa làm.
- ⏳ Phase 6: Meta Graph, import đối soát và analytics chưa bật.
- ⏳ Phase S0–S7: chuyển 17 bảng, media và dashboard identity sang Supabase
  Postgres/Storage/Auth; chi tiết trong `PLAN_SUPABASE.md`.

## Nguyên tắc

- Local-only, một người dùng, tối đa hai Page/tài khoản thử nghiệm ở MVP.
- Agent là copilot; publisher worker là deterministic.
- Chỉ nội dung đã duyệt mới được lên lịch hoặc đăng.
- Secret không đi vào prompt, trace, Telegram hay database; database chỉ giữ `auth_ref`.
- MockPublisher phải hoàn chỉnh trước khi bật adapter thật.
- Không xây auto-comment, fake engagement, fake order hoặc cơ chế né checkpoint.

## Phase 0 — Fork và baseline

- Clone từ `Laplace_Demon`, giữ Git history và đặt remote nguồn là `upstream`.
- Tạo branch `arya-main`.
- Đổi project identity và default database.
- Khóa baseline bằng toàn bộ test hiện có.

**Done khi:** 238 test gốc pass trong môi trường riêng.

## Phase 1 — Social domain

- Models: account, media, affiliate product, social post, publish job, publish attempt.
- Pydantic schemas và status transition.
- Cấu hình publisher/timezone/daily limit.
- Database initialization đăng ký social metadata.

**Done khi:** tạo được toàn bộ bảng trên SQLite sạch và validation chặn state/input sai.

## Phase 2 — Deterministic publishing

- `SocialPublisher` contract.
- `MockPublisher` với scripted outcomes.
- Publish service/worker claim job nguyên tử, khóa theo account và dùng idempotency key.
- Retry chỉ cho network/rate-limit/5xx; auth/checkpoint/input phải dừng.
- Recovery job bị treo sau restart.

**Done khi:** test chứng minh không đăng trùng qua retry, concurrent claim và restart.

## Phase 3 — Agent tools

- `social_account_status`
- `content_library`
- `schedule_social_post`
- `publish_social_post`
- `pause_social_account`
- `affiliate_report`

Create/update/delete/schedule/publish/pause phải đi qua confirm flow phù hợp.

**Done khi:** eval tiếng Việt chọn đúng tool, hỏi lại khi thiếu dữ liệu và không tự approve.

## Phase 4 — Local dashboard

- Overview, Accounts, Content, Calendar, Jobs.
- Upload media với MIME/size/hash validation.
- Draft → approve → schedule flow.
- Content Studio có 6 preset: Review thật thà, Deal ngắn gọn, Kể chuyện tình
  huống, So sánh để chọn mua, Hướng dẫn checklist và Script video ngắn.
- Copywriter dùng cấu hình riêng
  `LAPLACE_SOCIAL_CONTENT_PROVIDER=openrouter` và
  `LAPLACE_SOCIAL_CONTENT_MODEL=openrouter/free`; key được validate/lưu local,
  không render đầy đủ.
- Generate → deterministic validate → human approve; lỗi schema, độ dài,
  hashtag/emoji, URL và claim bị cấm không được qua approval gate.
- Kết nối Facebook và Instagram bằng browser profile cục bộ tách biệt theo
  user/account; chỉ lưu `auth_ref` opaque, không nhận cookie, token hoặc mật khẩu.
- API key/admin session bắt buộc cho write actions; CSRF cho form.

**Trạng thái:** Content Studio, draft/approve và form kết nối Facebook/Instagram
đã xong; Phase 4 tổng thể vẫn còn các form quản trị upload/schedule.

**Done khi:** hoàn thành vertical flow từ upload đến MockPublisher trên localhost.

## Phase 5 — Telegram

- `/accounts`, `/today`, `/queue`, `/pause`, `/resume`, `/report`.
- Preview + inline confirm trước schedule/publish.
- Thông báo success/failure/checkpoint và daily summary.

**Done khi:** chỉ Telegram owner có thể confirm và secret không xuất hiện trong message.

## Phase 6 — Real adapters và analytics

- Meta Graph API cho Page trước.
- Browser-profile adapter chỉ là opt-in experiment, tách biệt credential và tự dừng khi checkpoint.
- Import CSV affiliate theo batch, map `sub_id` về post/job.
- Metrics: commission/post, EPC khi có click, top product/account/time.

**Done khi:** một Page thử nghiệm đăng thành công qua API, import CSV đối soát đúng và toàn bộ hardening tests pass.

## Release gates

1. Mock gate: không có adapter thật trước khi idempotency/concurrency/recovery pass.
2. Approval gate: agent không được approve hoặc publish ngoài confirm flow.
3. Credential gate: không raw secret trong DB/log/trace/Telegram.
4. Platform gate: adapter thật chỉ thao tác tài sản do người dùng quản lý.
5. Regression gate: test gốc và social tests đều phải pass trước mỗi merge.
