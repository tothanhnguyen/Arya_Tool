# Arya_Tool

**Arya_Tool** là công cụ local-first kế thừa agent core của
Laplace's Demon để quản lý nội dung, lịch đăng và báo cáo affiliate. Agent đóng
vai trò copilot: tạo bản nháp, đề xuất lịch và giải thích số liệu; mọi hành động
đăng bài vẫn đi qua nội dung đã duyệt, publish job xác định và lớp publisher có
chống đăng trùng.

> Trạng thái hiện tại: MVP local với MockPublisher đã chạy được từ upload media
> → draft → approve → schedule → worker. Adapter Meta thật vẫn đang khóa
> fail-closed. Không dùng bản này để tự động rải bình luận, tạo tương tác hoặc
> đơn hàng giả.

📋 **Kế hoạch triển khai:** [PLAN_ARYA_TOOL.md](PLAN_ARYA_TOOL.md)

## Nền tảng kế thừa

**Laplace's Demon** là một AI Agent cá nhân giao tiếp qua Telegram, thực hiện trọn vẹn vòng lặp *nhận yêu cầu → phân tích → lập kế hoạch → gọi công cụ → quan sát → điều chỉnh → trả lời*. Tên dự án là một **ẩn dụ** lấy từ thí nghiệm tư duy của Pierre-Simon Laplace về một thực thể biết toàn bộ trạng thái hiện tại và từ đó suy ra hành động tiếp theo — ở đây tượng trưng cho khả năng **quan sát trạng thái, lập kế hoạch và thực thi dựa trên thông tin hiện có** của agent, chứ không phải tuyên bố hệ thống "biết mọi thứ". Trọng tâm của dự án là độ tin cậy, khả năng quan sát (full execution trace) và đánh giá định lượng, thay vì chỉ là lớp vỏ gọi API LLM. Stack: Python + FastAPI + aiogram + SQLite, LLM hỗ trợ **8 hãng qua preset registry** (Gemini, OpenAI, Groq, OpenRouter, DeepSeek, xAI, Mistral, Ollama local — kèm mock provider chạy offline).

📚 **Tài liệu:** [Hướng dẫn cài đặt & sử dụng](docs/HUONG_DAN.md) · [Kiến trúc chi tiết](docs/KIEN_TRUC.md) · [Kế hoạch tổng thể](PLAN.md)

## Kiến trúc

```
Telegram ⇄ aiogram Bot ⇄ FastAPI Backend
                              │
                        ┌─────▼──────┐
                        │  Task API   │  (tạo task, trạng thái, confirm)
                        └─────┬──────┘
                              │
                     ┌────────▼─────────┐
                     │ Agent Orchestrator│  ← state machine
                     │  (agent loop)     │
                     └──┬────────────┬──┘
                        │            │
                 ┌──────▼────┐  ┌────▼────────┐
                 │ LLM Layer  │  │Tool Executor │→ Tool Registry (9 tools)
                 │ (adapters) │  └────┬────────┘
                 └───────────┘        │
                              ┌───────▼────────┐
                              │  Storage (DB)   │ conversations / tasks /
                              │  + Trace Store  │ steps / traces / profile
                              └───────┬────────┘
                                      │
                     Scheduler (APScheduler)   Trace Viewer (web, read-only)
```

## Cấu trúc thư mục

```
laplace/
├── __main__.py          # Entrypoint: python -m laplace
├── config.py            # Cấu hình (env LAPLACE_*, pydantic-settings)
├── db.py                # Kết nối SQLAlchemy
├── models.py            # Schema DB: tasks, steps, llm_calls, notes, todos...
├── schemas.py           # Pydantic schema cho structured output
├── agent/               # Agent loop (state machine) + strategies
├── llm/                 # LLM layer
│   ├── base.py          #   Protocol LLMProvider + LLMResult + factory (đọc preset registry)
│   ├── presets.py       #   Preset 8 hãng: base_url, env key, model mặc định, bảng giá
│   ├── setup.py         #   Wizard dán API key: python -m laplace.llm.setup
│   ├── check.py         #   Kiểm tra key sống/chết: python -m laplace.llm.check [--all]
│   ├── mock.py          #   MockLLM — chạy dev/test không cần API key
│   └── openai_provider.py  # Adapter OpenAI-compatible cho mọi preset (token, cost, latency, retry 429)
├── services/            # Task service + trace store
├── evals/               # Eval harness (python -m laplace.evals)
└── tools/               # Tool registry + các tool
evals/cases/             # Bộ test case YAML cho eval harness
tests/                   # pytest
PLAN.md                  # Kế hoạch chi tiết của đồ án
```

## Cài đặt từng bước

### 0. Yêu cầu hệ thống

- **Python ≥ 3.11** (đã test với 3.12–3.14) — chạy local; hoặc **Docker + Docker Compose ≥ 2.24** nếu chạy container.
- macOS / Linux (Windows dùng WSL).
- Không bắt buộc key nào để chạy thử: mặc định dùng **mock provider** (offline). Muốn dùng thật thì cần: token Telegram bot (miễn phí), API key Gemini (có free tier) hoặc OpenAI, Tavily key cho tìm kiếm web (tùy chọn).

### 1. Cài đặt local (venv + pip)

```bash
git clone <repo-url> && cd Arya_Tool
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # cài package + pytest/ruff
```

### 2. Cấu hình `.env`

```bash
cp .env.example .env
```

Mặc định `LAPLACE_LLM_PROVIDER=mock` — **chạy được ngay, không cần điền gì**. Muốn dùng thật, mở `.env` và điền:

| Muốn gì | Điền gì |
|---|---|
| Bot Telegram thật | `LAPLACE_TELEGRAM_BOT_TOKEN` — chat với [@BotFather](https://t.me/BotFather), gõ `/newbot`, đặt tên → nhận token dạng `123456:ABC-...` |
| LLM Gemini (free tier) | `LAPLACE_LLM_PROVIDER=gemini` + `LAPLACE_GEMINI_API_KEY` — tạo key tại [Google AI Studio](https://aistudio.google.com/apikey) |
| LLM OpenAI | `LAPLACE_LLM_PROVIDER=openai` + `LAPLACE_OPENAI_API_KEY` |
| Copywriter Content Studio | `LAPLACE_OPENROUTER_API_KEY` + `LAPLACE_SOCIAL_CONTENT_PROVIDER=openrouter` + `LAPLACE_SOCIAL_CONTENT_MODEL=openrouter/free` |
| Tìm kiếm web thật | `LAPLACE_SEARCH_API_KEY` (Tavily) — trống thì tool `web_search` trả kết quả stub |

Chi tiết đầy đủ các biến: [docs/HUONG_DAN.md §2](docs/HUONG_DAN.md).

### Chuẩn bị Supabase (chưa cần key để chạy local)

Repository đã có sẵn:

- schema 18 bảng tại `supabase/migrations/`, gồm metadata artifact;
- RLS theo owner và hai bucket private `arya-media`, `arya-artifacts`;
- kết nối SQLAlchemy/psycopg có SSL, pool và startup check;
- Storage adapter media cùng artifact service private/signed URL;
- ETL SQLite → Postgres/Storage có inventory/reconciliation, mặc định chỉ dry-run;
- SQLite vẫn là mặc định cho đến khi bạn tự điền thông tin Supabase.

Sau khi tạo project, điền các biến sau vào `.env` trên máy của bạn. Không gửi
secret key qua chat và không commit `.env`:

```env
LAPLACE_DB_URL=postgresql://...
LAPLACE_SUPABASE_URL=https://<project-ref>.supabase.co
LAPLACE_SUPABASE_SECRET_KEY=
LAPLACE_SUPABASE_PUBLISHABLE_KEY=
LAPLACE_SUPABASE_AUTH_ENABLED=true
LAPLACE_SOCIAL_MEDIA_BACKEND=supabase
```

`PUBLISHABLE_KEY` (hoặc legacy `ANON_KEY`) chỉ dùng để kiểm tra Supabase Auth
session. `SECRET_KEY` chỉ ở server cho Storage; ứng dụng từ chối dùng service
key làm user-auth key. Khi Auth được bật, dashboard map `auth.users.id` vào
`public.users.auth_user_id` và fail closed nếu session thiếu, hết hạn hoặc chưa
được map. Access token có thể gửi bằng Bearer hoặc cookie do login layer đặt
với `HttpOnly`, `Secure` và `SameSite`; JSON mutation bắt buộc Bearer, còn form
dashboard dùng CSRF/same-origin gate.

Link project và dựng schema:

```bash
npx --yes supabase@latest login
npx --yes supabase@latest link --project-ref <project-ref>
npx --yes supabase@latest db push --dry-run
npx --yes supabase@latest db push
```

Kiểm kê SQLite trước khi ghi lên Supabase:

```bash
python -m laplace.migrations.sqlite_to_supabase \
  --source sqlite:///./arya-tool.db \
  --dry-run \
  --report reports/supabase-dry-run.json
```

Lệnh sinh cả báo cáo JSON và Markdown, không in URL/password hay nội dung row.
Sau khi backup SQLite và thư mục `media/`, chạy thật bằng cách đổi `--dry-run`
thành `--execute`. ETL giữ nguyên ID, ghi theo batch/foreign key, kiểm từng ID,
reset identity sequence và dùng SHA-256 để media chạy lại không bị nhân đôi.

Chi tiết cutover/rollback: [PLAN_SUPABASE.md](PLAN_SUPABASE.md).

### Nối API key hãng AI

Hệ thống dùng **preset registry** (`laplace/llm/presets.py`) — hỗ trợ 8 hãng có endpoint tương thích OpenAI, "dán key là chạy":

| Hãng (`LAPLACE_LLM_PROVIDER=`) | Trang lấy key | Free tier | Biến env chứa key |
|---|---|---|---|
| `gemini` — Google AI Studio | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Có, rộng | `LAPLACE_GEMINI_API_KEY` |
| `openai` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | Không | `LAPLACE_OPENAI_API_KEY` |
| `groq` — inference siêu nhanh | [console.groq.com/keys](https://console.groq.com/keys) | Có, nhanh | `LAPLACE_GROQ_API_KEY` |
| `openrouter` — 1 key → trăm model (kể cả Claude) | [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) | Có (model `:free`) | `LAPLACE_OPENROUTER_API_KEY` |
| `deepseek` | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) | Không (giá rẻ) | `LAPLACE_DEEPSEEK_API_KEY` |
| `xai` — Grok | [console.x.ai](https://console.x.ai/) | Không | `LAPLACE_XAI_API_KEY` |
| `mistral` | [console.mistral.ai/api-keys](https://console.mistral.ai/api-keys) | Có (giới hạn rate) | `LAPLACE_MISTRAL_API_KEY` |
| `ollama` — chạy LOCAL | [ollama.com/download](https://ollama.com/download) (cài app) | Miễn phí | *(không cần key)* |

**Cách 1 — wizard (khuyên dùng):**

```bash
python -m laplace.llm.setup
```

Chọn hãng theo số → dán key (không hiện ra màn hình, không lọt shell history) → wizard gọi thử 1 request kiểm tra key sống (in latency + model trả lời) → mới ghi `.env` (sửa đúng dòng, giữ comment, backup `.env.bak`, `chmod 600`; key đã có thì hỏi trước khi đè) và đặt luôn `LAPLACE_LLM_PROVIDER`.

**Cách 2 — tự sửa `.env`:** điền 2 dòng `LAPLACE_LLM_PROVIDER=<hãng>` + `LAPLACE_<HÃNG>_API_KEY=<key>` (tùy chọn `LAPLACE_LLM_MODEL=` để đổi model), rồi kiểm tra:

```bash
python -m laplace.llm.check        # provider đang chọn: key sống/chết + latency
python -m laplace.llm.check --all  # thử mọi hãng đã có key trong .env
```

Thiếu key thì thông báo lỗi luôn kèm URL trang lấy key của đúng hãng đó. Key khi in ra màn hình luôn được che (6 ký tự đầu + `...`).

### 3. Chạy bot

```bash
python -m laplace
```

Một lệnh khởi động cả web (API + trace viewer, http://localhost:8010/), Telegram bot (nếu có token) và scheduler. Mở Telegram, tìm bot của bạn, gõ `/start` rồi nhắn yêu cầu tự nhiên ("Lưu ghi chú: deadline 30/8", "So sánh FastAPI và Flask, viết báo cáo ngắn"...). Dừng bằng `Ctrl+C` (graceful shutdown).

Các màn hình social local:

- Dashboard: `http://127.0.0.1:8010/social/`
- Kết nối Facebook: `http://127.0.0.1:8010/social/facebook/connect`
- Kết nối Instagram: `http://127.0.0.1:8010/social/instagram/connect`
- Affiliate Analytics: `http://127.0.0.1:8010/stats`
- API tương tác và upload: `http://127.0.0.1:8010/docs`
- Telegram: `/accounts`, `/today`, `/queue`, `/pause <id>`,
  `/resume <id>`, `/report`

Facebook và Instagram dùng browser profile riêng cho từng user/account. Arya_Tool
chỉ mở trang đăng nhập chính thức để bạn tự đăng nhập; form không nhận cookie,
mật khẩu hoặc access token, và ứng dụng không đọc/xuất cookie. Trạng thái
`ready` chỉ được ghi sau khi bạn xác nhận đúng tài khoản. Đây là bước kết nối
cục bộ; publisher thật vẫn bị khóa.

Mặc định publisher là `mock`, nên worker mô phỏng đăng thành công mà không gọi
Facebook/TikTok. Tạo account thử bằng `POST /api/social/accounts/mock`, upload
JPEG/PNG/WebP/MP4, tạo draft, approve và schedule qua nhóm endpoint
`/api/social/*`. Nếu đặt `LAPLACE_API_KEY`, mọi request phải gửi header
`X-API-Key`.

## Content Studio — viết content bằng OpenRouter Free

Mở **http://127.0.0.1:8010/social/content** để tạo nội dung affiliate từ dữ
kiện sản phẩm đã kiểm tra. Copywriter dùng cấu hình riêng, không thay đổi
`LAPLACE_LLM_PROVIDER` hoặc model của agent:

```env
LAPLACE_OPENROUTER_API_KEY=sk-or-v1-...
LAPLACE_SOCIAL_CONTENT_PROVIDER=openrouter
LAPLACE_SOCIAL_CONTENT_MODEL=openrouter/free
```

Có thể nhập và validate key tại **http://127.0.0.1:8010/settings**. Settings chỉ
truy cập từ localhost, gửi key trong POST body và chỉ hiển thị dạng che; không
đưa raw key vào HTML, prompt, trace hay database.

Content Studio có 6 preset văn phong:

1. **Review thật thà** — ưu, điểm cần cân nhắc và người phù hợp.
2. **Deal ngắn gọn** — lợi ích, điều kiện/giới hạn và CTA nhanh.
3. **Kể chuyện tình huống** — kể tình huống giả định, không bịa đã mua/đã dùng.
4. **So sánh để chọn mua** — so sánh theo tiêu chí và nhóm nhu cầu.
5. **Hướng dẫn checklist** — checklist ngắn trước khi mua.
6. **Script video ngắn** — hook 2 giây, ba ý chính, lưu ý và CTA.

Luồng bắt buộc là **generate → validate → approve**:

1. Model chỉ tạo draft từ sản phẩm, media, đối tượng đọc, dữ kiện và preset đã chọn.
2. Arya_Tool kiểm tra bằng code: schema JSON, độ dài, số hashtag/emoji, URL và
   các claim bị cấm; draft lỗi được sửa lại tối đa theo cấu hình retry.
3. Nội dung hợp lệ vẫn ở trạng thái `draft`. Người dùng phải xem và bấm
   **Duyệt nội dung** trước khi có thể lên lịch hoặc đăng.

`openrouter/free` tự định tuyến qua model miễn phí đang khả dụng, nên phù hợp
cho MVP và tác vụ content đơn giản nhưng chất lượng, độ trễ và khả năng trả đúng
JSON có thể dao động. Tài khoản free thường chỉ có khoảng 50 request/ngày và
rate limit/quota có thể thay đổi; khi cần đầu ra ổn định nên ghim một model cụ
thể hoặc chuyển sang gói trả phí.

### 4. Chạy test & eval

```bash
pytest                     # toàn bộ test offline, không cần key
python -m laplace.evals    # eval harness: 37 case qua agent loop thật (mock, offline)

# Số liệu thật (cần key) — lặp 3 lần/case, có thể thêm LLM-as-judge:
python -m laplace.evals --provider gemini --runs 3 --judge openai
```

Kết quả nằm trong `eval_results/<timestamp>-<provider>/` (`results.json` + `report.md`). Chi tiết: [docs/HUONG_DAN.md §9](docs/HUONG_DAN.md).

### 5. Chạy bằng Docker

```bash
cp .env.example .env       # tùy chọn — không có .env vẫn chạy được ở chế độ mock
docker compose up --build
```

- SQLite nằm trong volume `./data/` (`/app/data/arya-tool.db` trong container), báo cáo markdown trong `./reports/` — dữ liệu giữ nguyên qua các lần restart.
- Container có **healthcheck** liveness tại `/health/live`; readiness DB,
  Storage và scheduler ở `/health/ready`.
- Preflight vận hành không in secret: chạy
  `docker compose run --rm --no-deps arya_tool python -m laplace.ops.preflight config`
  trước khi start và `python -m laplace.ops.preflight runtime` sau khi start.
- Trace viewer: http://localhost:8010/ như khi chạy local.

Runbook health, SQLite backup và kế hoạch backup Supabase:
[docs/OPS_RUNBOOK.md](docs/OPS_RUNBOOK.md).

### 6. CI/CD

GitHub Actions chạy `ruff check .`, toàn bộ `pytest` offline và Docker build cho
mọi pull request vào `main`. Khi push `main` hoặc tag `v*`, cùng image đã qua
gate được publish lên GitHub Container Registry:

```bash
docker pull ghcr.io/tothanhnguyen/arya_tool:latest
```

Workflow chỉ dùng `GITHUB_TOKEN` do GitHub cấp, không cần commit thêm secret.

## Bộ tool (9 tool MVP)

| Tool | Chức năng | Ghi/Đọc | Cần confirm |
|---|---|---|---|
| `web_search` | Tìm kiếm web (Tavily; không có key thì chạy stub) | Đọc | Không |
| `fetch_page` | Tải và trích nội dung chính của 1 URL | Đọc | Không |
| `note_store` | CRUD ghi chú của user | Ghi | Có (update/delete) |
| `task_list` | CRUD việc cần làm | Ghi | Có (delete hàng loạt) |
| `report_builder` | Ghép các observation thành báo cáo markdown | Ghi file | Không |
| `scheduler` | Tạo/xóa job định kỳ | Ghi | Có |
| `social_account` | Xem, pause/resume tài khoản social | Ghi | Pause/resume |
| `social_content` | List, tạo draft, approve/delete nội dung | Ghi | Approve/delete |
| `social_schedule` | List, tạo/hủy publish job | Ghi | Tạo/hủy |

## Affiliate Analytics

Trang **http://localhost:8010/stats** tổng hợp lượt xem, lượt bấm, CTR,
hoa hồng, EPC và tỷ lệ chuyển đổi. Dữ liệu được ghi nhận qua
`POST /api/social/events`; trang Eval nội bộ không còn hiển thị trong menu.

## Giới hạn hiện tại & roadmap

Giới hạn của MVP:

- Chưa có adapter Meta Graph/TikTok thật; chỉ `MockPublisher` được bật.
- Dashboard local đã có form ghi dữ liệu; Supabase Auth session vẫn chưa nối,
  nên database nhiều owner sẽ fail closed ở `/stats`.
- Analytics affiliate cần dữ liệu import CSV hoặc event từ nguồn đối soát;
  chưa tự gọi API của affiliate network.
- Scheduler chạy in-process (APScheduler), chưa phân tán (Celery/Redis).
- Chống prompt injection ở mức cơ bản (delimiter + system prompt).
- SQLite vẫn là backend local mặc định; adapter/migration Supabase đã chuẩn bị,
  còn link project, push schema, Auth session và cutover cần credential của bạn.

Roadmap chi tiết theo tuần (kiến trúc, eval harness 2 chiến lược × 2 model, hardening, deploy): xem [PLAN.md](PLAN.md).
# Laplace-Demon-Beta
# Arya_Tool
