# Arya_Tool - Ke hoach 4 cua so Codex song song

> Cap nhat: 2026-07-27
>
> Muc tieu cua dot nay la hoan thanh cac phan local con thieu trong
> `PLAN_ARYA_TOOL.md` va `PLAN_SUPABASE.md`, voi moi cua so so huu mot vung file
> rieng. Khong cho nhieu Codex cung sua truc tiep mot thu muc repository.

## Phan cong hien tai

| Cua so | Task | Trang thai |
|---|---|---|
| 1 | Dashboard write forms | DA XONG - agent `dashboard_forms`, 2026-07-28 |
| 2 | Telegram notification va daily summary | DA XONG - `/root`, 2026-07-28 |
| 3 | Supabase readiness va artifact storage | DA XONG - agent `supabase_readiness`, 2026-07-28 |
| 4 | Affiliate import va analytics | DA XONG - `/root`, 2026-07-28 |
| 5 | CI test gate va CD container image | DA XONG - `/root`, 2026-07-28 |

## Dot 2 - Auth, Artifact API va van han

| Phase | Task | Owner | Vung file chinh | Trang thai |
|---|---|---|---|---|
| A | Supabase Auth session va dashboard owner mapping | agent `dashboard_forms` | `laplace/web/supabase_auth.py`, `laplace/web/deps.py`, `laplace/models.py`, `laplace/db.py`, `laplace/config.py`, test Auth moi | DA XONG - 2026-07-28 |
| B | Owner-scoped Artifact API/runtime integration | agent `supabase_readiness` | `laplace/web/artifacts.py`, `laplace/services/artifacts.py`, test Artifact API moi | DA XONG - 2026-07-28 |
| C | S7 health/readiness va backup tooling an toan | agent `quality_audit` | `laplace/ops/`, `laplace/web/health.py`, test Ops/health/backup moi | DA XONG - 2026-07-28 |
| D1 | Enforce Auth owner tren task/social/connect va CSRF API | agent `dashboard_forms` | task/trace/social/connect web routes va test owner integration | DA XONG - 2026-07-28 |
| D2 | Tich hop router/config/schema, review va full gate | `/root` | `laplace/web/app.py`, cac file plan va xung dot tich hop | DA XONG - 2026-07-28 |

Ranh gio Dot 2:

- Ca ba phase A/B/C phai test offline bang dependency injection/fake; khong
  doc hoac sua `.env`, khong goi remote Supabase.
- Phase A dung publishable/anon key cho user session, khong dung service key;
  khi Auth bat thi session sai/thieu phai fail closed, khi Auth tat thi giu
  local fallback ro rang.
- Phase B lay owner tu authenticated request context, khong tin owner ID trong
  body/form; upload/download/signed URL/delete/reconcile khong lo secret hoac
  loi upstream.
- Phase C tach liveness/readiness; output health khong chua URL, credential,
  exception text hay object name. Backup SQLite co hash/manifest/no-overwrite;
  Postgres/Supabase chi lap ke hoach command, khong execute.
- Agent khong stage/commit. `/root` review, tich hop, chay full gate va commit
  mot lan sau khi ca ba phase dat gate.

Ket qua Dot 2:

- Dashboard validate Supabase Auth session bang publishable/anon key, map UUID
  sang owner noi bo va fail closed khi Auth bat; local/API-key fallback van
  hoat dong khi Auth tat.
- Task, trace, social dashboard/API va Facebook/Instagram connect da scope theo
  authenticated owner. JSON mutation dung Bearer; HTML mutation co CSRF hoac
  same-origin gate.
- Artifact API private co upload/download/signed URL/delete/reconciliation,
  validation MIME/size/content va local single-owner fallback. URL Supabase chi
  cho HTTPS, tru loopback emulator.
- `/health/live` tach khoi `/health/ready`; readiness kiem DB/schema, Storage,
  scheduler backlog/retry/lag. SQLite backup co online snapshot, SHA-256,
  manifest/no-overwrite; Supabase backup helper chi sinh plan, khong execute.
- Full gate offline: 517 pass, 1 remote Postgres test skip; Ruff va
  `git diff --check` pass.

Quy uoc trang thai dung chung cho moi bang task:

- `TASK TRONG`: chua co owner; terminal khac duoc phep nhan.
- `DANG NHAN TASK`: da co owner va dang xu ly; terminal khac khong duoc sua
  vung file cua task nay.
- `DA XONG`: owner da hoan thanh, ghi ket qua/test/commit va khong con thay doi
  dang do. Terminal tich hop van phai review gate truoc khi merge.

Moi terminal phai cap nhat dong task tu `TASK TRONG` sang `DANG NHAN TASK`,
kem ten agent va thoi diem, **truoc khi sua file**. Chi duoc nhan task dang
`TASK TRONG`. Neu bo task, owner phai ghi ly do va dua trang thai ve
`TASK TRONG`; khong de task mac ket o `DANG NHAN TASK`.

Ket qua cua so 1:

- Them form tao mock account, upload media, tao product/draft, approve,
  schedule va cancel; toan bo POST co CSRF, loopback/API-key gate va owner
  relation check.
- MIME/size/input/state transition duoc validate; loi storage/auth khong bi
  phan chieu ra HTML.
- 30 focused test pass; social regression va Ruff pass.

Ket qua cua so 2:

- Gui Telegram cho `published`, terminal `failed`, checkpoint va auth-expired;
  retry/deferred im lang de khong spam.
- Terminal DB row la nguon retry ben vung sau send failure/restart; tracking
  baseline tranh replay lich su cu.
- Daily summary 20:00 theo `social_timezone` co same-day catch-up/retry sau gio
  cau hinh; dedupe check/send/mark duoc serialize trong process va khoa row
  tren Postgres.
- 27 test notification/scheduler/worker/Telegram pass.

Ket qua cua so 3:

- Artifact Storage private, owner-scoped, deterministic key, signed URL ngan
  han, compensation, deletion audit va reconciliation.
- Them migration artifact/RLS, schema revision health fail-closed, ETL
  inventory artifact/media va checklist cutover/rollback khong thay doi remote.
- 21 focused test pass, 1 remote Postgres test skip dung thiet ke.

Ket qua cua so 4:

- CSV importer atomic, idempotent va owner-scoped; ho tro `job:<id>`,
  `post:<id>` va publish-job idempotency key trong `sub_id`.
- `/stats` tach analytics service, loc theo owner va co top
  content/product/account/local-hour; EPC khong co click hien `—`, khong hien 0.
- Money dung `Decimal`/`NUMERIC(18,6)`; event ID unique theo owner; duplicate
  conflict/race rollback atomic; metadata co secret/resource-limit hardening.
- `/stats` fail closed khi database co nhieu user ma chua co owner context.
- 49 focused test importer/model/API/analytics/stats pass.

Ket qua task 5:

- Moi branch push va pull request vao `main` chay Ruff + full pytest offline.
- Pull request/workflow thu cong build image read-only; chi push `main` hoac
  tag `v*` moi co quyen publish `ghcr.io/tothanhnguyen/arya_tool`.
- Image mac dinh bind `0.0.0.0:8010`, luu SQLite trong `/app/data`, chay bang
  user khong phai root; build context loai secret/runtime data.
- Workflow YAML va Compose config pass static gate. Full suite tich hop:
  457 pass, 1 remote Postgres test skip; Docker daemon local dang tat nen image
  build se duoc gate tren GitHub Actions truoc khi publish.

## 0. Trang thai xuat phat

- Branch hien tai: `arya-main`.
- Snapshot local hien co 392 test pass va `ruff check .` pass.
- Social MVP, Content Studio, Facebook/Instagram browser profile va Supabase
  scaffold dang la thay doi chua commit.
- `.venv/bin/pytest` co shebang cu tu `Laplace_Affiliate`; tam thoi chay
  `.venv/bin/python3.14 -m pytest`, hoac tao lai venv trong tung worktree.

Vi thay doi chua commit khong xuat hien trong worktree moi, terminal chinh phai
review va tao mot commit checkpoint truoc khi chia viec. Khong stash roi cho
bon cua so lam tu commit cu.

```bash
cd /Users/thanhnguyen/Documents/Arya_Tool

.venv/bin/python3.14 -m pytest -q
.venv/bin/ruff check .
git status --short
git diff --check

# Review ky danh sach file truoc khi commit; .env phai tiep tuc bi ignore.
git add -A
git commit -m "feat: checkpoint Arya social and Supabase foundation"
```

## 1. Tao 4 worktree

Chay sau khi checkpoint da duoc commit:

```bash
cd /Users/thanhnguyen/Documents/Arya_Tool

git worktree add ../Arya_Tool-web       -b feat/arya-web-forms arya-main
git worktree add ../Arya_Tool-telegram  -b feat/arya-telegram-notify arya-main
git worktree add ../Arya_Tool-supabase  -b feat/arya-supabase-readiness arya-main
git worktree add ../Arya_Tool-analytics -b feat/arya-affiliate-analytics arya-main
```

Trong moi worktree:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Khong copy `.env` sang cac worktree. Tat ca test cua dot nay phai chay bang
mock/fake. Credential Supabase hoac Meta chi duoc dung o terminal chinh khi
nguoi dung chu dong cau hinh.

## 2. Cua so 1 - Dashboard write forms

Thu muc: `../Arya_Tool-web`

Muc tieu:

- Hoan thanh Phase 4 local dashboard: tao mock account, upload media, tao
  affiliate product/draft, approve, schedule va cancel ngay tren web.
- Tat ca POST form co CSRF, owner isolation, validate MIME/size/input va hien
  thong bao loi an toan.
- Giu Content Studio va hai trang connect hien tai hoat dong.
- Khong hardcode secret va khong dua `auth_ref` vao HTML.

So huu file:

- `laplace/web/social/views.py`
- `laplace/web/social/templates/social_accounts.html`
- `laplace/web/social/templates/social_content.html`
- `laplace/web/social/templates/social_calendar.html`
- `laplace/web/social/templates/social_jobs.html`
- File helper moi trong `laplace/web/social/` neu can
- `tests/test_social_forms.py` va test web moi cua luong nay

Khong sua:

- `laplace/web/app.py`, `laplace/config.py`, `laplace/db.py`
- `laplace/bot/`, `laplace/migrations/`, `laplace/social/storage.py`
- `laplace/web/statsview.py`, `laplace/web/templates/stats.html`
- Cac file connect Facebook/Instagram

Prompt dan vao Codex:

> Doc `PLAN_ARYA_TOOL.md` Phase 4 va code trong `laplace/web/social/`. Hoan
> thanh cac write form local tu account/media/product/draft den
> approve/schedule/cancel. Chi sua vung file duoc giao trong
> `PLAN_SONG_SONG.md`. Tai su dung service/API hien co, giu owner isolation,
> CSRF va validation upload; khong them raw secret/auth_ref vao HTML. Viet test
> cho happy path, CSRF sai, cross-owner, MIME sai va transition sai. Chay test
> lien quan, sau do full pytest + ruff, commit tren branch hien tai. Khong merge.

Gate:

- Mot nguoi dung co the hoan thanh vertical flow bang browser voi
  `MockPublisher`.
- CSRF va owner isolation co negative test.

## 3. Cua so 2 - Telegram notification va daily summary

Thu muc: `../Arya_Tool-telegram`

Muc tieu:

- Hoan thanh phan con lai cua Phase 5.
- Gui notification cho publish success, terminal failure va checkpoint/auth
  failure; retry/deferred khong spam.
- Daily summary theo timezone cau hinh, chi gui cho owner Telegram da link.
- Bao toan owner-only confirm pause/resume va sau restart khong gui trung.

So huu file:

- `laplace/bot/social_handlers.py`
- `laplace/bot/runner.py`
- Module notification moi trong `laplace/bot/`
- `laplace/social/scheduler.py`
- `tests/test_social_telegram.py`
- Test notification/daily summary moi

Khong sua:

- `laplace/web/`, `laplace/config.py`, `laplace/db.py`
- `laplace/social/models.py`, `laplace/social/storage.py`
- `laplace/migrations/`, `supabase/`

Prompt dan vao Codex:

> Doc `PLAN_ARYA_TOOL.md` Phase 5 va cac test Telegram/social hien co. Them
> notification publish success/failure/checkpoint va daily summary, dung
> dependency injection de test khong can Telegram that. Khong spam cho
> retry/deferred, chi owner da link moi nhan, va restart khong gui trung trong
> cung chu ky. Chi sua vung file duoc giao trong `PLAN_SONG_SONG.md`. Viet test
> fake bot/clock cho timezone, duplicate suppression va owner isolation. Chay
> test lien quan, full pytest + ruff, commit tren branch hien tai. Khong merge.

Gate:

- Tat ca message tu dong duoc test bang fake bot va fake clock.
- Khong can token Telegram de test.

## 4. Cua so 3 - Supabase readiness va artifact storage

Thu muc: `../Arya_Tool-supabase`

Muc tieu:

- Hoan thanh phan local con thieu cua S4/S5: abstraction cho
  `arya-artifacts`, private upload/download/signed URL va idempotent object key.
- Mo rong ETL/reconciliation cho artifact metadata neu schema hien tai can.
- Them schema/revision health check va integration-test profile cho Postgres;
  offline CI dung fake HTTP/DB, remote test phai opt-in.
- Viet checklist/script an toan cho dry-run, cutover va rollback; khong tu link,
  push hay reset Supabase remote.

So huu file:

- `laplace/migrations/`
- Module artifact/Supabase moi trong `laplace/services/`
- `supabase/migrations/` (chi them migration timestamp moi, khong sua file da co)
- `supabase/seed.sql` neu chi dung fake data
- `laplace/config.py`, `laplace/db.py`
- `tests/test_sqlite_to_supabase.py`, `tests/test_supabase_db.py`
- Test artifact storage moi

Khong sua:

- `laplace/web/`, `laplace/bot/`
- `laplace/social/models.py`, `laplace/social/worker.py`
- `laplace/web/statsview.py`, cac social template

Prompt dan vao Codex:

> Doc `PLAN_SUPABASE.md`, uu tien S4 artifacts, S5 ETL/reconciliation va
> readiness gates. Chi sua vung file duoc giao trong `PLAN_SONG_SONG.md`.
> Implement artifact storage private voi object key deterministic, signed URL
> ngan han va compensation/idempotency; them schema health va Postgres
> integration profile opt-in. Tat ca default test phai offline bang fake
> HTTP/DB. Khong doc `.env`, khong login/link/push/reset Supabase remote, khong
> in URL/password/key. Chay test lien quan, full pytest + ruff, commit tren
> branch hien tai. Khong merge.

Gate:

- Unit test offline pass, remote integration bi skip neu chua co bien opt-in.
- Khong co lenh thay doi remote va khong co secret trong diff/log.

## 5. Cua so 4 - Affiliate import va analytics

Thu muc: `../Arya_Tool-analytics`

Muc tieu:

- Hoan thanh phan offline cua Phase 6: import CSV affiliate theo batch, map
  `sub_id` ve post/job va chay lai khong trung event.
- Metrics: commission/post, click, EPC khi co denominator, top
  product/account/time; khong bien missing click thanh 0 EPC.
- Noi ket qua that vao `/stats`, co empty state va filter owner/timezone.
- Khong goi Meta API va khong publish that trong dot nay.

So huu file:

- Module moi `laplace/social/affiliate_import.py`
- Module moi `laplace/social/analytics.py`
- `laplace/web/statsview.py`
- `laplace/web/templates/stats.html`
- Test import/analytics/stats cua luong nay
- Tai lieu format CSV moi trong `docs/` neu can

Khong sua:

- `laplace/web/social/`, `laplace/bot/`
- `laplace/config.py`, `laplace/db.py`, `laplace/social/models.py`
- `laplace/migrations/`, `supabase/`
- Publisher factory/worker

Prompt dan vao Codex:

> Doc `PLAN_ARYA_TOOL.md` Phase 6 va models affiliate hien co. Implement CSV
> import theo batch voi validation, `sub_id` mapping va idempotency; them
> analytics commission/post, click, EPC va top breakdown, sau do render tren
> `/stats`. Chi sua vung file duoc giao trong `PLAN_SONG_SONG.md`. Bao toan
> owner isolation, currency/timezone, missing denominator va empty state. Test
> malformed CSV, duplicate import, cross-owner va metric edge cases. Khong goi
> Meta API/publish that. Chay test lien quan, full pytest + ruff, commit tren
> branch hien tai. Khong merge.

Gate:

- Import cung file hai lan khong nhan doi event.
- Analytics co test doi soat tong va EPC.

## 6. Luat phoi hop

1. Truoc khi bat dau, terminal doc bang task moi nhat va chi claim dong co
   trang thai `TASK TRONG`. Viec claim phai doi trang thai thanh
   `DANG NHAN TASK`, ghi owner/thoi diem va duoc luu truoc moi thay doi code.
2. Moi cua so chi sua vung file duoc giao. Neu can sua file ngoai vung, dung
   lai va ghi de xuat trong commit message/bao cao, khong tu sua.
3. Khong sua `PLAN_ARYA_TOOL.md` de tu danh dau xong; terminal tich hop cap nhat
   plan sau khi verify.
4. Khong commit `.env`, database, media, browser profile, report chua redact
   hoac Supabase temp state.
5. Moi branch ket thuc bang:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
git diff --check
git status --short
git commit
```

6. Cua so nao xong thi ghi ket qua, commit hash, danh sach test va cac viec co
   y de lai, sau do moi doi trang thai sang `DA XONG`.

## 7. Thu tu merge

Tai terminal chinh:

```bash
cd /Users/thanhnguyen/Documents/Arya_Tool

git merge --no-ff feat/arya-supabase-readiness
git merge --no-ff feat/arya-affiliate-analytics
git merge --no-ff feat/arya-telegram-notify
git merge --no-ff feat/arya-web-forms

.venv/bin/python3.14 -m pytest -q
.venv/bin/ruff check .
git diff --check
```

Sau moi merge, chay test lien quan cua branch do. Neu full suite vo sau merge,
fix tren branch `arya-main` bang mot commit integration rieng; khong quay lai
sua lich su branch.

Sau khi toan bo gate pass:

- Cap nhat trang thai Phase 4/5/6 va S4/S5 trong hai plan.
- Chay smoke flow browser + MockPublisher tren `arya-main`.
- Moi sang dot 2: Supabase Auth dashboard session/RLS integration va Meta Graph
  adapter that. Hai viec nay can credential/quyet dinh tich hop va khong nen
  sua song song voi bon luong tren.

Don worktree chi sau khi da merge va verify:

```bash
git worktree remove ../Arya_Tool-web
git worktree remove ../Arya_Tool-telegram
git worktree remove ../Arya_Tool-supabase
git worktree remove ../Arya_Tool-analytics
```
