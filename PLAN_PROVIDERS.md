# Plan: Nối API key của nhiều hãng AI — "dán key là chạy"

> Mục tiêu UX: người dùng lấy API key từ hãng AI phổ biến (Google AI Studio, OpenAI,
> Groq, OpenRouter, DeepSeek...), dán vào `.env`, đặt 1 biến chọn hãng → chạy ngay.
> Không sửa code, không cần hiểu kiến trúc bên trong.
>
> Thời điểm: SAU v1.0 (đồ án đã đóng băng tính năng) — làm trên branch `feat/providers`.

## Vì sao rẻ: hiện trạng đã sẵn 80%

- `laplace/llm/openai_provider.py` — `OpenAIProvider(api_key, model, base_url, name)`
  đã generic; Gemini đang chạy qua chính nó bằng endpoint OpenAI-compatible.
- Hầu hết hãng phổ biến có endpoint OpenAI-compatible → **không cần client mới**,
  chỉ cần preset `{base_url, env key, model mặc định, bảng giá}`.
- Ngoại lệ đáng kể duy nhất: **Anthropic (Claude)** — API format riêng
  (mức 3, hoặc khuyên dùng qua OpenRouter).

> **Tiến độ (đợt 1 — 27/07, branch worktree T15):** ✅ Mức 1 xong toàn bộ
> (presets 8 hãng + `get_provider()` theo registry + bảng giá per-preset +
> lỗi thiếu key kèm URL + `LAPLACE_LLM_MODEL` override). ✅ Mức 2: wizard
> `python -m laplace.llm.setup` + `python -m laplace.llm.check [--all]` + README.
> ⏳ Còn lại (đợt 2): lệnh Telegram `/model`, Mức 3 (failover, Anthropic native,
> chạy lại thí nghiệm đa provider).

## Mức 1 — Preset registry (nửa ngày) ⭐ lõi — ✅ ĐÃ LÀM (đợt 1)

1. `laplace/llm/presets.py`:

| preset | base_url | env key | model mặc định | ghi chú |
|---|---|---|---|---|
| `gemini` | generativelanguage.googleapis.com/v1beta/openai | `LAPLACE_GEMINI_API_KEY` | gemini-3.1-flash-lite | đã có — key lấy ở **Google AI Studio** |
| `openai` | (mặc định SDK) | `LAPLACE_OPENAI_API_KEY` | gpt-4o-mini | đã có |
| `groq` | api.groq.com/openai/v1 | `LAPLACE_GROQ_API_KEY` | llama-3.3-70b-versatile | free tier nhanh — ứng viên thay Gemini chạy eval |
| `openrouter` | openrouter.ai/api/v1 | `LAPLACE_OPENROUTER_API_KEY` | tùy chọn | 1 key → trăm model (kể cả Claude) |
| `deepseek` | api.deepseek.com | `LAPLACE_DEEPSEEK_API_KEY` | deepseek-chat | rẻ |
| `xai` | api.x.ai/v1 | `LAPLACE_XAI_API_KEY` | grok-3-mini | |
| `mistral` | api.mistral.ai/v1 | `LAPLACE_MISTRAL_API_KEY` | mistral-small-latest | |
| `ollama` | localhost:11434/v1 | (không cần key) | llama3.2 | chạy LOCAL — demo không cần mạng |

2. `.env` chỉ cần 2 dòng: `LAPLACE_LLM_PROVIDER=groq` + `LAPLACE_GROQ_API_KEY=gsk_...`
   (tùy chọn `LAPLACE_LLM_MODEL=` để override model).
3. `get_provider()` đọc preset thay vì if/else cứng như hiện tại; giữ nguyên
   interface `LLMProvider` → **agent/eval/budget/streaming không đổi 1 dòng nào**.
4. Lỗi thiếu key phải in kèm **URL trang lấy key** của đúng hãng đó
   (vd gemini → aistudio.google.com/apikey) — đây chính là phần "dễ dùng".
5. Bảng giá USD/1M token per-model (phục vụ cost tracking T9); model lạ → giá 0 + warning 1 lần.

## Mức 2 — UX kiểm tra & chuyển đổi (1 ngày) — ✅ MỘT PHẦN (đợt 1)

- ✅ `python -m laplace.llm.setup` (wizard, thêm ngoài plan): chọn hãng đánh số →
  dán key qua `getpass` (không echo, không nhận key qua tham số CLI) → validate
  key sống bằng 1 request nhỏ (in latency + model) → mới ghi `.env` (sửa đúng dòng,
  giữ comment, hỏi trước khi đè key cũ, backup `.env.bak`, chmod 600) + đặt luôn
  `LAPLACE_LLM_PROVIDER`. Key in ra luôn mask (6 ký tự đầu + "...").
- ✅ `python -m laplace.llm.check`: gọi 1 request nhỏ → báo key sống/chết, latency,
  model khả dụng. Dán key xong chạy 1 lệnh là biết ngay. Kèm `--all` thử mọi hãng có key.
- ⏳ Lệnh Telegram `/model` (chỉ admin): xem provider+model đang dùng, đổi runtime.
  **KHÔNG nhận key qua chat** — key chỉ nằm trong `.env` (lý do: message lưu vào DB/trace,
  key sẽ bị ghi lại; bảo mật ghi rõ trong docs).
- ✅ README: bảng "hãng | trang lấy key | free tier | biến env" cho 8 preset
  (mục "Nối API key hãng AI").

## Mức 3 — Nâng cao (tùy chọn) — ⏳ CHƯA LÀM (đợt 2)

- **Failover**: danh sách provider dự phòng `LAPLACE_LLM_FALLBACK=groq,ollama` —
  hết quota/429 quá N lần thì tự chuyển hãng kế tiếp (đúng bài học đêm 26/07
  khi Gemini free tier cạn 500 req/ngày giữa chừng thí nghiệm).
- **Anthropic native adapter** (API riêng) — chỉ làm nếu không muốn đi đường OpenRouter.
- **Chạy lại thí nghiệm**: `python -m laplace.evals.experiment --providers groq,mock ...`
  chạy được ngay nhờ preset → trả món nợ "so sánh model đầy đủ" trong future work
  của báo cáo, số liệu bổ sung được cho bảo vệ.

## Bảo mật & kiểm thử

- Key chỉ ở `.env` (đã gitignore); mask key khi log (`gsk_...abc`); không đưa vào trace.
- Test: preset load đúng base_url/env; thiếu key → message hướng dẫn đúng hãng;
  bảng giá tính đúng; mock cho từng preset (không gọi mạng trong CI).

## Thứ tự đề xuất

1. Mức 1 (presets + get_provider + giá) → 2. `llm.check` → 3. README → 4. `/model` → 5. failover.
Ước tổng: 1.5–2 ngày dev + test. Không đụng `laplace/agent/*`.
