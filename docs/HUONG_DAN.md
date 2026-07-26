# Laplace's Demon — Hướng dẫn cài đặt & sử dụng

> Tài liệu cho người chạy và demo hệ thống. Kiến trúc bên trong xem [KIEN_TRUC.md](KIEN_TRUC.md), kế hoạch tổng thể xem [../PLAN.md](../PLAN.md).

## 1. Yêu cầu

- Python ≥ 3.11 (đã test với 3.14)
- macOS / Linux (Windows dùng WSL)
- Tùy chọn: Docker, tài khoản Telegram, OpenAI API key, Tavily API key

## 2. Cài đặt

```bash
git clone <repo-url> && cd Laplace_Demon
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Mở `.env` và điền theo nhu cầu — **tất cả đều có thể để trống** để chạy thử:

| Biến | Bắt buộc? | Ý nghĩa |
|---|---|---|
| `LAPLACE_LLM_PROVIDER` | không (mặc định `mock`) | `mock` = chạy không cần key, trả lời giả lập; `openai` = LLM thật |
| `LAPLACE_OPENAI_API_KEY` | khi provider=openai | Key OpenAI |
| `LAPLACE_TELEGRAM_BOT_TOKEN` | khi muốn chạy bot | Lấy từ @BotFather (`/newbot`) |
| `LAPLACE_SEARCH_API_KEY` | không | Tavily key; trống thì `web_search` trả kết quả stub |
| `LAPLACE_API_KEY` | không (nên đặt khi deploy) | Bảo vệ REST API + trace viewer bằng header `X-API-Key` |
| `LAPLACE_MAX_STEPS` / `LAPLACE_TOOL_TIMEOUT_S` / `LAPLACE_TASK_TIMEOUT_S` | không | Giới hạn an toàn của agent loop |

## 3. Chạy

```bash
.venv/bin/python -m laplace
```

Một lệnh khởi động cả 3 thành phần:

- **Web** (API + trace viewer): http://localhost:8000/
- **Telegram bot** (chỉ khi có token): polling tự động
- **Scheduler**: nạp các job định kỳ từ database

Không có token Telegram → app vẫn chạy web + scheduler, log sẽ nhắc.

### Chạy bằng Docker

```bash
docker compose up --build
# SQLite được giữ trong ./data/ nhờ volume mount
```

## 4. Dùng qua Telegram

1. Tìm bot của bạn trên Telegram, gõ `/start`.
2. Nhắn yêu cầu bằng ngôn ngữ tự nhiên, ví dụ:
   - "So sánh FastAPI và Flask, viết báo cáo ngắn" *(tìm kiếm → đọc trang → viết báo cáo)*
   - "Lưu ghi chú: deadline proposal 30/8"
   - "Xóa hết việc đã xong" *(sẽ hỏi xác nhận trước khi xóa)*
   - "Mỗi sáng 8h tổng hợp tin AI gửi cho tôi" *(tạo job định kỳ — cần xác nhận)*
3. Với hành động ghi/xóa/tạo lịch, bot gửi nút **✅ Đồng ý / ❌ Từ chối** — chỉ **chủ task** bấm được (người khác trong group bấm sẽ bị từ chối).

Giới hạn: tin nhắn vào tối đa 2000 ký tự; trả lời dài được tự chia nhỏ theo giới hạn Telegram.

> ⚠️ Chế độ `mock`: bot chạy đủ luồng nhưng câu trả lời là giả lập và luôn được phân loại "trả lời trực tiếp" — muốn thấy agent chọn tool + nút xác nhận thì cần `LAPLACE_LLM_PROVIDER=openai`.

## 5. Trace viewer

Mở http://localhost:8000/ :

- **Trang danh sách**: 50 task gần nhất — trạng thái, chiến lược, tổng chi phí, thời gian chạy.
- **Trang chi tiết** (`/tasks/{id}`): timeline từng bước (tool, tham số, observation, độ trễ, retry) + bảng mọi lệnh gọi LLM (mục đích, model, token, cost) + tổng kết. Tự refresh 5 giây khi task đang chạy.

Đây là nơi trả lời câu hỏi "agent vừa làm gì và vì sao" — dùng khi demo và khi debug.

## 6. REST API

| Method | Endpoint | Mô tả |
|---|---|---|
| POST | `/api/tasks` | Tạo task: `{"request": "...", "strategy": "react"\|"plan_execute", "user_id": <tùy chọn>}` → 202, chạy nền |
| GET | `/api/tasks/{id}` | Trạng thái + kết quả |
| POST | `/api/tasks/{id}/confirm` | `{"approved": true\|false}` — duyệt/từ chối hành động đang chờ |
| GET | `/api/tasks/{id}/trace` | Toàn bộ trace JSON (steps, llm_calls, totals) |

Ví dụ:

```bash
curl -X POST localhost:8000/api/tasks \
  -H 'content-type: application/json' \
  -d '{"request": "Tìm hiểu về ReAct pattern", "strategy": "plan_execute"}'

curl localhost:8000/api/tasks/1/trace | python3 -m json.tool
```

Khi đặt `LAPLACE_API_KEY`, thêm header `-H 'X-API-Key: <key>'` vào mọi request (kể cả khi mở trace viewer — trình duyệt sẽ không truy cập được nếu thiếu; dùng extension đặt header hoặc chỉ đặt key khi deploy).

## 7. Tác vụ định kỳ

Job được tạo bằng cách **nhắn bot** ("mỗi sáng 8h...") hoặc ghi trực tiếp bảng `scheduled_jobs` (cron 5 trường + nội dung yêu cầu). Scheduler đọc job lúc khởi động; sau khi agent tạo/xóa job qua tool, gọi `laplace.scheduler.refresh_jobs()` hoặc restart app để nạp lại. Kết quả job được gửi về Telegram của chủ job.

## 8. Chạy test & lint

```bash
.venv/bin/python -m pytest -q     # 43 test, không cần mạng, không cần key
.venv/bin/ruff check laplace/ tests/
```

## 9. Sự cố thường gặp

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `TelegramConflictError: terminated by other getUpdates` lặp lại | Token đang bị một tiến trình khác polling (app cũ, máy khác) | Tắt tiến trình kia, hoặc `/revoke` token trong @BotFather rồi dùng token mới |
| `address already in use` cổng 8000 | Còn instance cũ chạy | `lsof -nP -iTCP:8000 -sTCP:LISTEN` → kill PID đó |
| Bot không trả lời | App chưa chạy / token sai / bot bị conflict | Xem log; `curl https://api.telegram.org/bot<TOKEN>/getMe` kiểm tra token |
| `database is locked` | Nhiều tiến trình ghi SQLite cùng lúc quá lâu | Đã bật WAL + busy_timeout; nếu vẫn gặp, kiểm tra có chạy 2 app trỏ cùng file DB không |
| Trả lời luôn có prefix `[mock]` | Đang chạy provider mock | Đặt `LAPLACE_LLM_PROVIDER=openai` + key rồi restart |
| 401 khi gọi API | `LAPLACE_API_KEY` đang bật | Thêm header `X-API-Key` |

## 10. Vị trí dữ liệu

- `laplace.db` (+ `-wal`, `-shm`): toàn bộ dữ liệu — task, trace, ghi chú, todo, job. Xóa các file này = reset sạch.
- `reports/`: file báo cáo markdown do tool `report_builder` tạo, tên dạng `u<user>-t<task>-<tiêu-đề>.md`.
- Cả hai đều nằm trong `.gitignore`.
