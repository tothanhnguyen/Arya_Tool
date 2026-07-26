# Laplace's Demon — Kế hoạch chi tiết (Detailed Master Plan)

> Phiên bản: 1.0 — 26/07/2026
> Dựa trên `Laplace_Demon_Project_Context_Handoff.md`. Tài liệu này **chốt các quyết định còn mở** (Mục 11 của context) bằng đề xuất cụ thể, kèm kế hoạch theo tuần, kiến trúc chi tiết và các đề xuất bổ sung.

---

## 0. Tóm tắt quyết định đề xuất (trả lời Mục 11 của context)

| Quyết định chưa chốt | Đề xuất | Lý do ngắn gọn |
|---|---|---|
| Use case chính | **"Trợ lý nghiên cứu & báo cáo cá nhân"** — tìm kiếm, đọc, tổng hợp thông tin nhiều nguồn thành báo cáo; quản lý ghi chú/công việc; tác vụ theo lịch | Đủ đa bước để thể hiện planning, tool phong phú nhưng an toàn (chủ yếu read-only), demo ổn định, không đụng hành động rủi ro cao |
| Người dùng mục tiêu | Sinh viên / người làm tri thức cần thu thập – tổng hợp thông tin định kỳ | Trùng với chính tác giả → dễ tạo test case thật |
| Danh sách tool MVP | 6 tool (xem §3.4): web_search, fetch_page, note_store, task_list, report_builder, scheduler | 3 read-only + 3 có ghi, đủ minh họa confirm-flow |
| Scheduler trong MVP? | **Có, dạng tối giản** (APScheduler in-process, không Celery/Redis ở MVP) | Use case "báo cáo định kỳ" là điểm demo mạnh; Celery để giai đoạn sau |
| Long-term memory? | MVP: conversation state + task state + **user profile key-value đơn giản**. Vector memory: mục mở rộng, không cam kết | Tránh phình scope; key-value profile đã đủ thể hiện khái niệm memory |
| LangGraph hay tự xây? | **Tự xây agent loop** (state machine ~300–500 dòng), tham khảo LangGraph về khái niệm | Yêu cầu học thuật: hiểu và bảo vệ được vòng lặp; tự xây là đóng góp trình bày được |
| Model LLM | Provider abstraction (1 interface, 2 adapter: OpenAI + 1 provider thứ hai hoặc model local qua Ollama) | Vừa tránh lock-in, vừa tạo được thí nghiệm so sánh model trong phần đánh giá |
| Tiêu chí đánh giá chính | Task success rate, tool-selection accuracy, số bước, latency, cost/task, recovery rate (bộ 30–50 test case cố định) | Xem §6 |
| Đóng góp riêng của đồ án | (1) Agent loop tự xây dạng state machine có trace đầy đủ; (2) **Trace viewer** trực quan; (3) **Bộ eval harness + so sánh 2 chiến lược agent (ReAct vs Plan-and-Execute) và 2 model** | Biến đồ án từ "wrapper" thành nghiên cứu thực nghiệm có số liệu |
| Web dashboard? | **Có, dạng read-only trace viewer** (1 trang FastAPI + HTMX/Jinja, không SPA) | Chi phí thấp, giá trị demo và chấm điểm rất cao |
| Nghiên cứu hay sản phẩm? | Cần hỏi giảng viên buổi đầu; plan này thiết kế để **đứng được cả hai hướng** (sản phẩm chạy được + thí nghiệm so sánh có số liệu) | Giảm rủi ro |

---

## 1. Problem statement & phạm vi

### 1.1. Problem statement (dùng cho proposal)

> Người dùng cá nhân thường xuyên phải thực hiện các tác vụ thông tin lặp lại: tìm kiếm trên nhiều nguồn, đọc – lọc – tổng hợp, ghi chú và theo dõi công việc. Chatbot truyền thống chỉ trả lời từng câu hỏi đơn lẻ, không tự **lập kế hoạch nhiều bước, gọi công cụ, quan sát kết quả và tự điều chỉnh**. Đồ án xây dựng **Laplace's Demon** — một AI Agent giao tiếp qua Telegram, thực hiện trọn vẹn vòng lặp *nhận yêu cầu → phân tích → lập kế hoạch → gọi công cụ → quan sát → điều chỉnh → trả lời*, với trọng tâm là **độ tin cậy, khả năng quan sát (observability) và đánh giá định lượng**, thay vì chỉ là lớp vỏ gọi API LLM.

### 1.2. In-scope (MVP)

- Telegram bot (aiogram) + FastAPI backend.
- Agent loop tự xây (state machine), hỗ trợ tác vụ 1 bước và nhiều bước (tối đa N bước cấu hình được).
- Tool registry + 6 tool, có schema, quyền, retry, log I/O.
- Conversation state + task state + user profile (SQLite → PostgreSQL).
- Human-in-the-loop: xác nhận qua inline button Telegram trước hành động có ghi/xóa.
- Scheduler tối giản cho tác vụ định kỳ.
- Full execution trace + trace viewer web read-only.
- Eval harness: 30–50 test case, đo 8 metric, so sánh 2 chiến lược agent × 2 model.
- Safety cơ bản: whitelist tool, step limit, timeout, rate limit, ẩn secret, lọc prompt injection cơ bản.

### 1.3. Out-of-scope (ghi rõ trong proposal để bảo vệ scope)

- Multi-agent, plugin marketplace, vector database / RAG quy mô lớn.
- Kênh ngoài Telegram (Slack, Discord, web chat).
- Thực thi lệnh hệ thống tùy ý, tự động hóa trình duyệt.
- Hạ tầng phân tán (Celery multi-worker, Kubernetes).

---

## 2. Use case cụ thể (kịch bản demo)

1. **Hỏi–đáp có tra cứu (1 bước):** "Tìm giúp tôi giá RAM DDR5 32GB hiện nay" → web_search → tổng hợp → trả lời kèm nguồn.
2. **Tổng hợp nhiều nguồn (nhiều bước):** "So sánh FastAPI và Flask cho backend đồ án, viết thành báo cáo ngắn" → search → fetch 2–3 trang → report_builder → trả file/markdown.
3. **Ghi chú & việc cần làm (có ghi, cần confirm):** "Lưu ghi chú: deadline proposal 30/8" / "Xóa hết task đã xong" → confirm → thực thi.
4. **Tác vụ định kỳ:** "Mỗi sáng 8h tổng hợp tin AI mới nhất và gửi cho tôi" → tạo scheduled job → hằng ngày agent tự chạy pipeline search+summarize → đẩy kết quả về Telegram.
5. **Phục hồi lỗi (kịch bản demo chủ đích):** một nguồn fetch lỗi → agent retry / đổi nguồn → vẫn hoàn thành → thể hiện trong trace viewer.

---

## 3. Kiến trúc chi tiết

### 3.1. Sơ đồ tổng thể

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

### 3.2. Agent loop — thiết kế state machine (đóng góp chính #1)

Các state: `RECEIVED → CLASSIFY → PLAN → EXECUTE_STEP → OBSERVE → (REPLAN | NEXT_STEP | AWAIT_CONFIRM | DONE | FAILED)`

Pseudocode:

```python
def run_task(task):
    ctx = load_context(task)                      # conversation + profile
    route = llm.classify(task.request, ctx)       # direct / single_tool / multi_step / clarify
    if route == "direct":   return llm.answer(...)
    if route == "clarify":  return ask_user(...)

    plan = llm.plan(task.request, tools.specs(), ctx)   # list[Step]
    for i in range(MAX_STEPS):                          # step limit
        step = plan.current()
        if step.tool.requires_confirmation and not step.confirmed:
            return await_user_confirm(step)             # inline button, resume sau
        obs = executor.run(step, timeout=TOOL_TIMEOUT)  # retry w/ backoff bên trong
        trace.record(step, obs)                         # full trace từng bước
        verdict = llm.evaluate(obs, plan, goal)         # done / continue / replan / fail
        if verdict == "done":   break
        if verdict == "replan": plan = llm.replan(plan, obs)
    return llm.final_answer(trace, goal)
```

Điểm nhấn kỹ thuật để bảo vệ trước hội đồng:

- **Resume được:** task state lưu DB sau mỗi bước → confirm của người dùng đến sau vài phút vẫn tiếp tục đúng chỗ.
- **Hai chiến lược cắm được vào cùng khung:** `ReActStrategy` (nghĩ–gọi từng bước) và `PlanExecuteStrategy` (lập kế hoạch trước, thực thi tuần tự) cùng implement một interface `AgentStrategy` → dùng cho thí nghiệm so sánh ở §6.
- **Structured output:** mọi lệnh gọi LLM ép JSON schema (function calling), validate bằng Pydantic; sai schema → retry với thông báo lỗi kèm theo (self-correction).

### 3.3. LLM Layer

```python
class LLMProvider(Protocol):
    def complete(self, messages, tools=None, json_schema=None) -> LLMResult: ...
# adapters: OpenAIProvider, (đề xuất) AnthropicProvider hoặc OllamaProvider
```

- Đếm token + chi phí mỗi call, ghi vào trace.
- Config chọn provider/model qua env → chạy eval trên 2 model không đổi code.

### 3.4. Tool Registry — 6 tool MVP

| Tool | Chức năng | Ghi/Đọc | Cần confirm |
|---|---|---|---|
| `web_search` | Tìm kiếm (Tavily/Brave/SerpAPI — API có free tier) | Đọc | Không |
| `fetch_page` | Tải và trích nội dung chính 1 URL (trafilatura/readability) | Đọc | Không |
| `note_store` | CRUD ghi chú của user | Ghi | Có (với update/delete) |
| `task_list` | CRUD việc cần làm | Ghi | Có (với delete hàng loạt) |
| `report_builder` | Ghép các observation thành báo cáo markdown/file | Ghi file | Không |
| `scheduler` | Tạo/xóa job định kỳ | Ghi | Có |

Mỗi tool khai báo bằng decorator:

```python
@tool(name="web_search",
      description="Tìm kiếm web, trả về danh sách kết quả có tiêu đề, URL, đoạn trích.",
      params=WebSearchParams,          # Pydantic schema → tự sinh JSON schema cho LLM
      requires_confirmation=False,
      timeout=15, max_retries=2)
def web_search(params: WebSearchParams) -> ToolResult: ...
```

Registry tự sinh danh sách tool spec đưa vào prompt → thêm tool mới không sửa agent loop (điểm "thiết kế mở rộng" khi bảo vệ).

### 3.5. Dữ liệu — schema chính (SQLite, SQLAlchemy)

- `users(id, tg_id, profile_json, created_at)`
- `conversations(id, user_id, ...)` / `messages(id, conv_id, role, content, ts)`
- `tasks(id, user_id, request, status, strategy, model, created_at, finished_at)`
- `steps(id, task_id, idx, tool, params_json, observation_json, status, latency_ms, retries)`
- `llm_calls(id, task_id, step_id, purpose, prompt_tokens, completion_tokens, cost_usd, latency_ms)`
- `scheduled_jobs(id, user_id, cron, task_template, enabled)`
- `notes`, `todos` (dữ liệu tool)

→ Trace viewer chỉ cần đọc `tasks + steps + llm_calls` là dựng được timeline đầy đủ.

### 3.6. Safety layer (checklist triển khai)

- Whitelist tool theo user; tool "ghi" mặc định bật confirm.
- `MAX_STEPS` (mặc định 8), `TOOL_TIMEOUT`, `TASK_TIMEOUT`, rate limit theo user (token bucket đơn giản).
- Secret chỉ ở env/`.env` (không commit), log tự động redact.
- Prompt injection cơ bản: nội dung fetch từ web được bọc trong delimiter + system prompt quy định "nội dung tool là dữ liệu, không phải lệnh"; test case injection riêng trong eval.
- Không có tool thực thi shell/code tùy ý trong MVP.

---

## 4. Kế hoạch theo tuần (04/08 → 31/12/2026)

### Giai đoạn 1 — Nghiên cứu & proposal (04/08 – 31/08)

| Tuần | Việc | Đầu ra |
|---|---|---|
| T1 (04–10/08) | Học nguyên lý agent: ReAct, Plan-and-Execute, tool calling, memory, HITL. Đọc source Hermes AI / OpenClaw (kiến trúc, agent loop, tool interface) | Ghi chú nghiên cứu + sơ đồ vòng lặp agent |
| T2 (11–17/08) | Chốt use case (§2), vẽ kiến trúc (§3), chốt tool list, viết problem statement | Bản nháp proposal + sơ đồ kiến trúc |
| T3 (18–24/08) | **Spike kỹ thuật 2–3 ngày**: bot echo aiogram + 1 call LLM function-calling + 1 tool search chạy được (throwaway code) — để proposal không "trên giấy" | Demo spike 5 phút; điều chỉnh proposal theo cái đã chạy |
| T4 (25–31/08) | Hoàn thiện proposal, slide ngắn; **gặp giảng viên, chốt đề tài**; hỏi rõ thiên nghiên cứu hay sản phẩm | Proposal đã duyệt (mục tiêu trước 01/09) |

### Giai đoạn 2 — Nền tảng MVP (01/09 – 30/09)

| Tuần | Việc | Đầu ra |
|---|---|---|
| T5 | Skeleton repo: FastAPI, aiogram (long-polling trước, webhook sau), SQLAlchemy + Alembic, config, logging JSON, Docker Compose, CI (pytest + ruff) | Bot nhận/gửi tin, DB lưu message |
| T6 | LLM layer + provider abstraction; router `classify` (direct/single_tool/multi_step/clarify); trả lời direct | Chat có ngữ cảnh hội thoại |
| T7 | Tool registry + executor (schema, timeout, retry); tool `web_search`, `fetch_page`; agent loop single-tool | Hỏi có tra cứu chạy end-to-end |
| T8 | Trace store đầy đủ (tasks/steps/llm_calls); agent loop multi-step (ReAct trước); `MAX_STEPS` + timeout | Use case "tổng hợp nhiều nguồn" chạy được, có trace trong DB |

### Giai đoạn 3 — Hoàn thiện thực thi (01/10 – 31/10)

| Tuần | Việc | Đầu ra |
|---|---|---|
| T9 | Tool `note_store`, `task_list`, `report_builder`; confirm-flow bằng inline button (task pause/resume) | Demo confirm hành động nhạy cảm |
| T10 | `PlanExecuteStrategy` (chiến lược thứ hai, cùng interface); replan khi observation lỗi; error taxonomy + retry/backoff | 2 chiến lược chạy song song được |
| T11 | Scheduler (APScheduler) + tool `scheduler`; job định kỳ đẩy kết quả về Telegram; user profile memory đơn giản | Use case báo cáo hằng ngày chạy |
| T12 | **Trace viewer web** (FastAPI + Jinja/HTMX): danh sách task → timeline bước → prompt/response/tokens/cost từng call; viết test cho core (registry, loop, confirm) | MVP demo nội bộ ổn định; coverage phần core |

### Giai đoạn 4 — Đánh giá & tối ưu (01/11 – 30/11)

| Tuần | Việc | Đầu ra |
|---|---|---|
| T13 | Xây eval harness: 30–50 test case YAML (request, tool mong đợi, tiêu chí đúng), runner tự động chạy + chấm (rule-based + LLM-as-judge cho chất lượng câu trả lời) | Bộ eval chạy 1 lệnh |
| T14 | **Thí nghiệm 2×2: {ReAct, Plan-Execute} × {model A, model B}**, ≥3 run/cấu hình; đo 8 metric §6 | Bảng số liệu + biểu đồ |
| T15 | Tối ưu theo số liệu: prompt, tool description, router; chạy lại eval để chứng minh cải thiện (before/after) | Bảng so sánh trước/sau tối ưu |
| T16 | Hardening: test injection, lỗi mạng, rate limit; sửa bug từ eval; đóng băng tính năng | Phiên bản gần hoàn chỉnh |

### Giai đoạn 5 — Báo cáo & bảo vệ (01/12 – 31/12)

| Tuần | Việc |
|---|---|
| T17 | Docker hóa hoàn chỉnh, deploy VPS, README + tài liệu cài đặt |
| T18 | Viết báo cáo (LaTeX, theo cấu trúc: vấn đề → kiến trúc → thực nghiệm → kết quả → giới hạn) |
| T19 | Slide + kịch bản demo 4 use case + **video demo dự phòng**; luyện phản biện (danh sách 20 câu hỏi dự kiến) |
| T20 | Buffer: sửa lỗi phút chót, tổng duyệt |

> **Nguyên tắc buffer:** mỗi giai đoạn chỉ lên kế hoạch ~80% quỹ thời gian; tính năng nào trễ >1 tuần so với mốc → cắt xuống mục "mở rộng" thay vì dời cả plan.

---

## 5. Định nghĩa "xong" cho MVP (Definition of Done)

1. 4 kịch bản demo ở §2 chạy ổn định 3 lần liên tiếp không lỗi.
2. Mọi task đều xem lại được trace đầy đủ trên trace viewer.
3. Hành động ghi/xóa luôn qua confirm; step limit và timeout hoạt động (có test).
4. Eval harness chạy tự động, xuất bảng metric; có kết quả so sánh 2 chiến lược × 2 model.
5. `docker compose up` dựng được toàn hệ thống từ máy sạch theo README.

---

## 6. Phương pháp đánh giá (đóng góp chính #3)

**Bộ test case:** 30–50 case cố định, phủ: trả lời trực tiếp / 1 tool / nhiều bước / cần confirm / cần clarify / lỗi tool giả lập / prompt injection. Mỗi case ghi: input, tool-sequence kỳ vọng (hoặc tập chấp nhận được), tiêu chí output.

**Metric (log tự động từ trace):**

1. Task success rate (%) — chấm rule-based + LLM-as-judge (có kiểm tra chéo tay ~20% mẫu).
2. Tool-selection accuracy (%).
3. Tham số tool hợp lệ ngay lần đầu (%).
4. Số bước trung bình / task.
5. Latency trung bình & p95.
6. Token + cost trung bình / task.
7. Recovery rate: % task gặp lỗi tool nhưng vẫn hoàn thành.
8. HITL rate: % task cần con người can thiệp ngoài confirm định trước.

**Thí nghiệm:** ma trận 2 chiến lược × 2 model, ≥3 run/cấu hình (LLM không xác định nên cần lặp); phân tích trade-off (ví dụ giả thuyết: Plan-Execute ít bước & rẻ hơn cho task tuyến tính, ReAct phục hồi lỗi tốt hơn). Đây là phần "học thuật" ăn điểm nhất — trình bày như một thực nghiệm có giả thuyết, phương pháp, kết quả.

---

## 7. Rủi ro & đối sách

| Rủi ro | Ảnh hưởng | Đối sách |
|---|---|---|
| Giảng viên duyệt trễ sau 01/09 | Trễ dây chuyền | Spike T3 giúp thuyết phục sớm; nếu trễ, giai đoạn 2 vẫn khởi động vì kiến trúc lõi không phụ thuộc use case |
| API search / LLM đổi giá, hết quota | Demo chết | Adapter cho 2 provider; cache kết quả search khi dev; quỹ chi phí ~10–20 USD/tháng, theo dõi qua metric cost |
| Agent lặp vô hạn / lạc đề | Tốn tiền, demo xấu | MAX_STEPS, task timeout, verdict evaluate mỗi bước — làm từ T8, không để cuối |
| Scope phình (thêm RAG, multi-agent…) | Không kịp | Mục out-of-scope viết hẳn vào proposal; mọi ý tưởng mới → ghi vào backlog "sau đồ án" |
| Demo trực tiếp lỗi mạng/API | Mất điểm | Video demo dự phòng (T19) + chế độ replay từ trace đã lưu |
| Solo, ốm/bận 1–2 tuần | Trễ mốc | Buffer 80% + T20 trống; core (loop, trace, eval) làm trước, tính năng phụ sau |

---

## 8. Đề xuất bổ sung (ngoài context gốc)

Xếp theo mức khuyến nghị:

### Nên đưa vào MVP

1. **Trace viewer web read-only** — chi phí ~1 tuần, giá trị demo/chấm điểm rất cao; biến "logging" thành "observability" trình diễn được.
2. **Spike kỹ thuật trước khi gặp giảng viên (T3)** — proposal kèm demo 5 phút thuyết phục hơn hẳn slide suông.
3. **Cost & token tracking per-task ngay từ đầu** — vừa là safety (đặt trần chi phí/task), vừa là dữ liệu cho phần đánh giá, gần như miễn phí nếu làm sớm.
4. **Chế độ replay trace** — chạy lại demo từ trace đã lưu khi mất mạng; cứu cánh ngày bảo vệ.
5. **Structured output + self-correction** (validate Pydantic, lỗi schema thì gửi lại lỗi cho LLM sửa) — giảm mạnh lỗi tham số tool, có số liệu trước/sau để kể trong báo cáo.

### Cân nhắc (làm nếu còn thời gian ở T15–T16)

6. **Tool interface theo chuẩn MCP (Model Context Protocol)** — thiết kế tool registry có adapter tương thích khái niệm MCP; điểm cộng "bắt kịp chuẩn công nghiệp", nhưng không nên phụ thuộc SDK ngay từ đầu.
7. **Streaming status về Telegram** — bot nhắn "🔍 Đang tìm kiếm… ✍️ Đang tổng hợp…" theo từng bước (đọc từ trace events); UX demo đẹp, chi phí thấp.
8. **Semantic cache cho web_search** trong lúc dev/eval — giảm chi phí và làm eval ổn định hơn (cùng input → cùng observation).

### Không nên làm trong đồ án (ghi vào "hướng phát triển")

- Vector DB / long-term semantic memory, multi-agent, plugin marketplace, kênh Slack/Discord, Celery + Redis phân tán, sandbox thực thi code. Mỗi mục một câu trong chương "Future work" của báo cáo là đủ.

---

## 9. Việc cần làm ngay tuần tới (trước 04/08)

1. Tạo repo GitHub `laplace-demon`, đưa file context + plan này vào.
2. Đăng ký API key: 1 LLM provider chính + 1 search API (ưu tiên loại có free tier).
3. Đặt lịch gặp giảng viên (mục tiêu trong tuần 25–31/08, đặt sớm để giữ chỗ).
4. Bắt đầu T1: đọc paper ReAct, tài liệu function calling của provider đã chọn, và lướt kiến trúc Hermes AI / OpenClaw.
