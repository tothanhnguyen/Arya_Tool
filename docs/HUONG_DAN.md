# Laplace's Demon — Hướng dẫn cài đặt & sử dụng

> Tài liệu cho người chạy và demo hệ thống. Kiến trúc bên trong xem [KIEN_TRUC.md](KIEN_TRUC.md), kế hoạch tổng thể xem [../PLAN.md](../PLAN.md).

## 1. Yêu cầu

- Python ≥ 3.11 (đã test với 3.14)
- macOS / Linux (Windows dùng WSL)
- Tùy chọn: Docker, tài khoản Telegram, OpenAI/Gemini API key, Tavily API key

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
| `LAPLACE_LLM_PROVIDER` | không (mặc định `mock`) | `mock` = chạy không cần key, trả lời giả lập; `openai` \| `gemini` = LLM thật |
| `LAPLACE_OPENAI_API_KEY` / `LAPLACE_OPENAI_MODEL` | khi provider=openai | Key OpenAI + model (mặc định `gpt-4o-mini`) |
| `LAPLACE_GEMINI_API_KEY` / `LAPLACE_GEMINI_MODEL` | khi provider=gemini | Key Gemini (lấy từ Google AI Studio, có free tier) + model (mặc định `gemini-3.1-flash-lite` — bản lite có quota free tier rộng); dùng qua endpoint tương thích OpenAI |
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

> ⚠️ Chế độ `mock`: bot chạy đủ luồng nhưng câu trả lời là giả lập và luôn được phân loại "trả lời trực tiếp" — muốn thấy agent chọn tool + nút xác nhận thì cần `LAPLACE_LLM_PROVIDER=openai` hoặc `gemini`.

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
.venv/bin/python -m pytest -q     # 48 test, không cần mạng, không cần key
.venv/bin/ruff check laplace/ tests/
```

## 9. Chạy đánh giá (eval harness)

Bộ đánh giá chạy các test case cố định qua **agent loop thật** rồi chấm rule-based và tổng hợp metric (success rate, tool-selection accuracy, cost, latency...). Chi tiết kiến trúc xem [KIEN_TRUC.md](KIEN_TRUC.md#8-eval-harness).

```bash
.venv/bin/python -m laplace.evals                            # mock, offline, deterministic — không cần key
.venv/bin/python -m laplace.evals --provider gemini --runs 3 # LLM thật, lặp 3 lần/case → số liệu thực
.venv/bin/python -m laplace.evals --provider openai --strategy plan_execute  # ép 1 chiến lược cho thí nghiệm
```

- `--provider mock|openai|gemini` — mock phát lại `mock_script` trong case (regression, không tốn tiền); provider thật bỏ qua `mock_script` và đo số liệu thực.
- `--runs N` — số lần chạy mỗi case (LLM không xác định nên nên ≥3 khi đo số liệu thật).
- `--strategy react|plan_execute` — ép mọi case chạy một chiến lược, dùng cho thí nghiệm so sánh (chỉ dùng với provider thật, vì `mock_script` được viết riêng cho chiến lược gốc của case).
- `--cases`, `--out` — đổi thư mục case / thư mục kết quả nếu cần.

**Output**: mỗi lần chạy tạo thư mục `eval_results/<timestamp>-<provider>/` chứa:

- `results.json` — meta + summary + kết quả từng run (checks, steps, tokens, cost, thời gian) — dùng cho phân tích/vẽ biểu đồ.
- `report.md` — bảng tổng hợp metric + bảng từng run + danh sách thất bại, đọc được ngay.
- Các file DB SQLite tạm (mỗi run một DB sạch) và `reports/` do tool sinh ra — nằm gọn trong thư mục kết quả.

**Viết case mới**: thêm mục vào một file YAML trong `evals/cases/` (xem 6 file sẵn có làm mẫu). Các field:

| Field | Ý nghĩa |
|---|---|
| `id`, `tags` | Định danh duy nhất + nhãn nhóm (`recovery` được tính riêng vào recovery_rate) |
| `request`, `strategy` | Yêu cầu đầu vào + chiến lược (`react` mặc định / `plan_execute`) |
| `expected.status` | Trạng thái cuối kỳ vọng (mặc định `done`) |
| `expected.route` | Nhãn phân loại kỳ vọng: `direct` / `clarify` / `single_tool` / `multi_step` |
| `expected.tools` + `tools_match` | Danh sách tool phải được thực thi; so khớp `set` (mặc định) / `exact` (đúng thứ tự) / `subset` |
| `expected.forbid_tools` | Tool KHÔNG được chạy (vd. case prompt injection) |
| `expected.answer_contains` | Các chuỗi phải xuất hiện trong câu trả lời cuối (không phân biệt hoa thường) |
| `confirm` | `approve` / `reject` — kịch bản người dùng bấm nút xác nhận |
| `patch_tools` | Giả lập tool hỏng: `{tool: fail_once\|fail_always}` — đo khả năng phục hồi |
| `seed` | Dữ liệu mồi trước khi chạy: `{notes: [...], todos: [...]}` |
| `mock_script` | Chuỗi phản hồi LLM cho MockLLM phát lại (chỉ dùng ở provider mock) |

## 10. Sự cố thường gặp

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `TelegramConflictError: terminated by other getUpdates` lặp lại | Token đang bị một tiến trình khác polling (app cũ, máy khác) | Tắt tiến trình kia, hoặc `/revoke` token trong @BotFather rồi dùng token mới |
| `address already in use` cổng 8000 | Còn instance cũ chạy | `lsof -nP -iTCP:8000 -sTCP:LISTEN` → kill PID đó |
| Bot không trả lời | App chưa chạy / token sai / bot bị conflict | Xem log; `curl https://api.telegram.org/bot<TOKEN>/getMe` kiểm tra token |
| `database is locked` | Nhiều tiến trình ghi SQLite cùng lúc quá lâu | Đã bật WAL + busy_timeout; nếu vẫn gặp, kiểm tra có chạy 2 app trỏ cùng file DB không |
| Trả lời luôn có prefix `[mock]` | Đang chạy provider mock | Đặt `LAPLACE_LLM_PROVIDER=openai` hoặc `gemini` + key rồi restart |
| 401 khi gọi API | `LAPLACE_API_KEY` đang bật | Thêm header `X-API-Key` |
| Lỗi 429 `RESOURCE_EXHAUSTED` với Gemini | Hết quota free tier — giới hạn tính **theo phút VÀ theo ngày**, mỗi model một quota riêng | Provider đã tự retry theo gợi ý "retry in Xs" của API (hết quota phút chỉ cần chờ); nếu hết quota **ngày** thì đổi sang model lite (`LAPLACE_GEMINI_MODEL=gemini-2.5-flash-lite`) hoặc chờ reset quota |

## 11. Vị trí dữ liệu

- `laplace.db` (+ `-wal`, `-shm`): toàn bộ dữ liệu — task, trace, ghi chú, todo, job. Xóa các file này = reset sạch.
- `reports/`: file báo cáo markdown do tool `report_builder` tạo, tên dạng `u<user>-t<task>-<tiêu-đề>.md`.
- `eval_results/`: kết quả các lần chạy eval harness (`results.json`, `report.md`, DB tạm).
- Tất cả đều nằm trong `.gitignore`.
