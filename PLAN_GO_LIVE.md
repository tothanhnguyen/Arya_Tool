# Arya_Tool - Kế hoạch đưa vào hoạt động

> Trạng thái: bản nháp để chốt cùng owner  
> Ngày: 2026-07-29  
> Base commit: `7372402`

## 1. Mục tiêu ưu tiên

Đưa Arya_Tool hiện tại vào hoạt động dưới dạng **private beta một owner** trước
khi mở thêm nhánh tool mới.

Private beta được xem là hoạt động khi:

- một runtime duy nhất chạy ổn định;
- dữ liệu pilot nằm trên SQLite và local media với backup/restore đã kiểm chứng;
- owner dùng được Telegram và dashboard để tạo, duyệt và lên lịch nội dung;
- một Facebook Page thử nghiệm đăng được qua Meta Graph API chính thức;
- chỉ nội dung đã duyệt mới được đăng;
- giới hạn tối đa hai bài mỗi ngày và cooldown vẫn được thực thi;
- restart/retry không tạo bài trùng;
- readiness, backup và cảnh báo vận hành có kiểm chứng;
- có rollback về `MockPublisher` mà không mất dữ liệu.

## 2. Trạng thái hiện tại

### Đã sẵn sàng

- Luồng local upload -> draft -> approve -> schedule -> worker.
- `MockPublisher`, idempotency, retry, recovery và intent fence.
- Telegram social commands và notification.
- Supabase schema, RLS, private Storage, owner mapping và staging smoke.
- Health/readiness, preflight, backup tooling và operations runbook.
- CI, Docker image và full offline regression suite.

### Chưa đủ để go-live thật

- `meta_graph` vẫn fail closed; chưa có publisher gọi API chính thức.
- Chưa có resolver production cho `auth_ref` trỏ tới Page credential.
- Luồng Facebook hiện tại chỉ chuẩn bị browser profile; không cấp Page token cho
  Meta Graph publisher.
- Dashboard kiểm tra được Supabase session nhưng chưa có login/logout layer cho
  người dùng cuối.
- Chưa chốt runtime pilot là máy Mac luôn bật hay VPS.
- Chưa chạy canary publish thật, backup schedule và alert thực tế.

## 3. Phạm vi private beta

### In scope

- Một owner.
- Một Facebook Page do owner quản lý.
- Post ảnh đơn + caption + affiliate URL.
- Human approval bắt buộc.
- Telegram và dashboard private.
- SQLite và local media cho pilot đầu; Supabase/VPS là milestone sau.
- Một application replica.
- Meta Graph API chính thức.

### Chưa làm trong milestone này

- TikTok, Threads hoặc nhiều Page.
- Video/Reels nếu ảnh đơn chưa ổn định.
- Multi-tenant public signup.
- Browser automation để đăng bài.
- Tự tạo đơn, tương tác, comment hoặc hành vi né checkpoint.
- QA03B live-driver remote automation.
- Tool nhận link sản phẩm và tự trả affiliate link.

## 4. Hai quyết định owner cần chốt

### A. Runtime pilot

Khuyến nghị: **Mac private pilot trước**, sau đó mới chuyển VPS.

Lý do:

- dashboard giữ loopback, chưa cần public domain/TLS;
- macOS Keychain phù hợp để giữ Page token ngoài database;
- Telegram polling và scheduler chạy được trong cùng một process;
- giảm số biến thay đổi khi canary Meta publisher.

Nếu chọn VPS ngay, cần bổ sung reverse proxy/TLS, firewall, secret backend,
deployment user, backup schedule và external monitoring trước canary.

### B. Dashboard access trong pilot

Khuyến nghị: private loopback + API key cho pilot đầu; Supabase Auth login UI là
gate trước khi dashboard được mở qua mạng.

Không được expose cổng `8010` ra Internet chỉ với API key.

## 5. Critical path

### Phase G0 - Chốt operating boundary

- Chốt Mac pilot hay VPS.
- Chốt Facebook Page thử nghiệm.
- Chốt post ảnh đơn là format canary đầu tiên.
- Chốt dashboard chỉ loopback trong pilot.

**Exit:** mọi task implementation dùng cùng một boundary.

### Phase G1 - Meta publisher core

- Thêm `CredentialResolver` chỉ nhận `auth_ref`.
- Thêm resolver Keychain cho Mac pilot; token không đi vào DB/log/trace.
- Thêm `MetaGraphPublisher` với timeout, no redirect, error taxonomy và response
  redaction.
- Pin API version sau khi đối chiếu tài liệu Meta chính thức tại thời điểm làm.
- Hỗ trợ account check và một ảnh + caption/link.
- Factory vẫn fail closed nếu resolver hoặc cấu hình thiếu.

**Exit:** fake-transport tests chứng minh success, auth failure, checkpoint,
rate limit, network error, invalid request và redaction.

### Phase G2 - Page onboarding

- Tạo operator-only setup command.
- Dùng input ẩn, không nhận token qua Telegram/dashboard.
- Xác minh Page identity và quyền publish qua API chính thức.
- Lưu token vào Keychain; DB chỉ lưu `keychain://...`.
- Tạo hoặc cập nhật đúng một `SocialAccount`.
- Không overwrite credential/account cũ nếu owner chưa xác nhận.

**Exit:** account check thật pass mà không lộ token hoặc Page identifier nhạy
cảm trong log.

### Phase G3 - Pilot runtime

- Chạy static preflight dành cho SQLite/local-media pilot.
- Khởi động đúng một replica.
- Xác nhận `/health/live` và `/health/ready`.
- Xác nhận Telegram bot, scheduler, SQLite và local media.
- Cấu hình service supervision và graceful restart.
- Bật backup cho SQLite và media, rồi kiểm tra một restore drill cô lập.

**Exit:** runtime sống qua restart và không có stale/duplicate scheduled work.

### Phase G4 - Canary publish

1. Tạo media và product test.
2. Tạo draft.
3. Owner duyệt thủ công.
4. Schedule một bài canary.
5. Worker đăng qua Meta Graph.
6. Đối chiếu remote post, local job và attempt.
7. Restart runtime và xác nhận không đăng lại.
8. Thử một failure có kiểm soát và rollback publisher về mock.

**Exit:** một canary thật thành công, không duplicate, cleanup/rollback rõ ràng.

### Phase G5 - Private go-live

- Mở cho owner dùng hằng ngày.
- Giữ một Page, hai bài/ngày và một replica.
- Theo dõi readiness, failed/retry/unknown attempts và backup.
- Có manual reconciliation cho outcome `unknown`.
- Chỉ sau một giai đoạn vận hành ổn định mới xét VPS/public dashboard.

## 6. Go/no-go gates

Không go-live nếu một trong các điều sau còn tồn tại:

- publisher không dùng Meta API chính thức;
- raw credential xuất hiện trong DB, log, trace, exception hoặc test artifact;
- dashboard bị expose công khai khi chưa có Supabase Auth login + HTTPS;
- readiness không xanh;
- nhiều hơn một scheduler replica;
- backup chưa chạy hoặc restore chưa được diễn tập;
- canary tạo duplicate;
- job `unknown` tự retry thay vì yêu cầu reconciliation;
- rollback về mock không hoạt động.

## 7. Phân công song song

### David

`GOLIVE-META-01`: Meta publisher core + credential resolver contract, chỉ dùng
fake transport trong giai đoạn implementation.

### Lucy

`GOLIVE-OPS-01`: xác minh operating boundary, threat model credential, runtime
pilot checklist và audit độc lập commit của David.

Sau khi hai task PASS mới tạo task `GOLIVE-META-02` cho onboarding thật và
`GOLIVE-RUNTIME-01` cho canary runtime.

Checklist vận hành chi tiết của Lucy là gate nguồn cho G00-G17. Những việc
R01-R04 và R09 phải được đóng hoặc có operator control tương đương trước canary.
R05-R08 chỉ chặn VPS/Supabase public và không chặn Mac loopback pilot.

## 8. Nhánh tiếp theo sau go-live

Tên tạm: `AFFILIATE-LINK-01`.

Luồng mục tiêu:

1. user gửi URL sản phẩm;
2. hệ thống chuẩn hóa và xác minh domain/network;
3. adapter affiliate network tạo deeplink;
4. hệ thống gắn owner/sub-id và lưu provenance;
5. trả affiliate link đã kiểm tra;
6. không giả link nếu network chưa hỗ trợ hoặc credential/quyền thiếu.

Nhánh này chỉ bắt đầu sau khi private go-live đạt đủ gate ở trên.
