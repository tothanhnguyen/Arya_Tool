# Laplace's Demon

**Laplace's Demon** là một AI Agent cá nhân giao tiếp qua Telegram, thực hiện trọn vẹn vòng lặp *nhận yêu cầu → phân tích → lập kế hoạch → gọi công cụ → quan sát → điều chỉnh → trả lời*. Tên dự án là một **ẩn dụ** lấy từ thí nghiệm tư duy của Pierre-Simon Laplace về một thực thể biết toàn bộ trạng thái hiện tại và từ đó suy ra hành động tiếp theo — ở đây tượng trưng cho khả năng **quan sát trạng thái, lập kế hoạch và thực thi dựa trên thông tin hiện có** của agent, chứ không phải tuyên bố hệ thống "biết mọi thứ". Trọng tâm của dự án là độ tin cậy, khả năng quan sát (full execution trace) và đánh giá định lượng, thay vì chỉ là lớp vỏ gọi API LLM.

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
│   ├── base.py          #   Protocol LLMProvider + LLMResult + factory
│   ├── mock.py          #   MockLLM — chạy dev/test không cần API key
│   └── openai_provider.py  # Adapter OpenAI (token, cost, latency)
├── services/            # Task service + trace store
└── tools/               # Tool registry + các tool
tests/                   # pytest
PLAN.md                  # Kế hoạch chi tiết của đồ án
```

## Chạy local

Yêu cầu: Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env     # mặc định LAPLACE_LLM_PROVIDER=mock — chạy được ngay, KHÔNG cần API key
python -m laplace
```

Muốn dùng LLM thật: mở `.env`, đặt `LAPLACE_LLM_PROVIDER=openai` và điền `LAPLACE_OPENAI_API_KEY`. Muốn chạy Telegram bot thì điền thêm `LAPLACE_TELEGRAM_BOT_TOKEN`.

## Chạy test

```bash
pytest
```

## Chạy bằng Docker

```bash
cp .env.example .env     # chỉnh sửa nếu cần
docker compose up --build
```

SQLite được lưu ở volume `./data` (`/app/data/laplace.db` trong container) nên dữ liệu giữ nguyên qua các lần restart.

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
