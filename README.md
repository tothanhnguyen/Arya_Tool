# Laplace's Demon

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
                 │ LLM Layer  │  │Tool Executor │→ Tool Registry (6 tools)
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
git clone <repo-url> && cd Laplace_Demon
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
| Tìm kiếm web thật | `LAPLACE_SEARCH_API_KEY` (Tavily) — trống thì tool `web_search` trả kết quả stub |

Chi tiết đầy đủ các biến: [docs/HUONG_DAN.md §2](docs/HUONG_DAN.md).

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

Một lệnh khởi động cả web (API + trace viewer, http://localhost:8000/), Telegram bot (nếu có token) và scheduler. Mở Telegram, tìm bot của bạn, gõ `/start` rồi nhắn yêu cầu tự nhiên ("Lưu ghi chú: deadline 30/8", "So sánh FastAPI và Flask, viết báo cáo ngắn"...). Dừng bằng `Ctrl+C` (graceful shutdown).

### 4. Chạy test & eval

```bash
pytest                     # 48 test, offline, không cần key
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

- SQLite nằm trong volume `./data/` (`/app/data/laplace.db` trong container), báo cáo markdown trong `./reports/` — dữ liệu giữ nguyên qua các lần restart.
- Container có **healthcheck** (gọi `/openapi.json` mỗi 30s) — `docker compose ps` hiện `healthy` sau ~20 giây.
- Trace viewer: http://localhost:8000/ như khi chạy local.

## Bộ tool (6 tool MVP)

| Tool | Chức năng | Ghi/Đọc | Cần confirm |
|---|---|---|---|
| `web_search` | Tìm kiếm web (Tavily; không có key thì chạy stub) | Đọc | Không |
| `fetch_page` | Tải và trích nội dung chính của 1 URL | Đọc | Không |
| `note_store` | CRUD ghi chú của user | Ghi | Có (update/delete) |
| `task_list` | CRUD việc cần làm | Ghi | Có (delete hàng loạt) |
| `report_builder` | Ghép các observation thành báo cáo markdown | Ghi file | Không |
| `scheduler` | Tạo/xóa job định kỳ | Ghi | Có |

## Trace viewer

Mọi task đều được ghi trace đầy đủ (từng bước, prompt/response, tokens, cost, latency). Xem tại **http://localhost:8000/** sau khi khởi động (local hoặc Docker).

## Giới hạn hiện tại & roadmap

Giới hạn của MVP:

- Chưa có multi-agent, vector memory / RAG, plugin marketplace.
- Chỉ hỗ trợ kênh Telegram (chưa có Slack/Discord/web chat).
- Scheduler chạy in-process (APScheduler), chưa phân tán (Celery/Redis).
- Chống prompt injection ở mức cơ bản (delimiter + system prompt).
- SQLite mặc định; PostgreSQL là hướng nâng cấp khi triển khai ổn định.

Roadmap chi tiết theo tuần (kiến trúc, eval harness 2 chiến lược × 2 model, hardening, deploy): xem [PLAN.md](PLAN.md).
# Laplace-Demon-Beta
