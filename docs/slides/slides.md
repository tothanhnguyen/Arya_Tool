---
marp: true
theme: default
paginate: true
lang: vi
title: "Laplace's Demon — AI Agent cá nhân qua Telegram"
description: "Slide bảo vệ đồ án tốt nghiệp"
style: |
  :root {
    --lp-ink: #1a2233;
    --lp-accent: #2456a6;
    --lp-muted: #5a6478;
    font-family: "Helvetica Neue", Arial, "Segoe UI", sans-serif;
  }
  section {
    color: var(--lp-ink);
    background: #fcfcfa;
    font-size: 26px;
    padding: 56px 64px;
  }
  h1 { color: var(--lp-accent); font-size: 1.5em; }
  h2 { color: var(--lp-accent); font-size: 1.15em; margin-top: 0.2em; }
  h3 { color: var(--lp-ink); font-size: 1.0em; }
  strong { color: var(--lp-accent); }
  em { color: var(--lp-muted); }
  table { font-size: 0.82em; }
  th { background: #eef2f8; color: var(--lp-ink); }
  code { background: #eef2f8; color: #24354f; }
  pre { font-size: 0.68em; line-height: 1.25; }
  blockquote { color: var(--lp-muted); border-left: 4px solid var(--lp-accent); font-size: 0.9em; }
  footer { color: var(--lp-muted); font-size: 0.55em; }
  section.lead { text-align: center; }
  section.lead h1 { font-size: 1.9em; }
  section.dense { font-size: 23px; }
  section.backup { background: #f4f1ea; }
  section.backup h2::before { content: "🛟 "; }
  .placeholder { color: #a6341f; font-weight: bold; }
footer: "Laplace's Demon — Đồ án tốt nghiệp · Thanh Nguyen"
---

<!-- _class: lead -->
<!-- _paginate: false -->
<!-- _footer: "" -->

# Laplace's Demon

## AI Agent cá nhân qua Telegram

Vòng lặp *nhận yêu cầu → phân tích → lập kế hoạch → gọi công cụ → quan sát → điều chỉnh → trả lời* — với trọng tâm **độ tin cậy, khả năng quan sát và đánh giá định lượng**

&nbsp;

**Người thực hiện: Thanh Nguyen**

*Đồ án tốt nghiệp · 12/2026*

---

## 1. Vấn đề & mục tiêu

**Vấn đề.** Người dùng cá nhân lặp lại các tác vụ thông tin: tìm kiếm nhiều nguồn, đọc – lọc – tổng hợp, ghi chú, theo dõi công việc. Chatbot truyền thống chỉ trả lời **từng câu đơn lẻ** — không tự lập kế hoạch nhiều bước, gọi công cụ, quan sát kết quả và tự điều chỉnh.

**Mục tiêu.** Xây dựng **Laplace's Demon** — agent "trợ lý nghiên cứu & báo cáo cá nhân" qua Telegram, và trả lời một câu hỏi thực nghiệm:

> Với cùng một hệ thống, **chiến lược agent nào** (ReAct vs Plan-and-Execute) và **model nào** cho kết quả tốt hơn — đo bằng số liệu, không cảm tính.

**3 đóng góp chính:**
1. Agent loop **tự xây dạng state machine** (~500 dòng, không framework) có trace đầy đủ
2. **Trace viewer** web + chế độ replay offline
3. **Eval harness** 66 case + thí nghiệm so sánh 2 chiến lược × 2 model

---

## 2. Kiến trúc tổng quan

```
Telegram ⇄ aiogram Bot ⇄ FastAPI Backend
                              │
                        ┌─────▼──────┐
                        │  Task API   │  (tạo task, trạng thái, confirm)
                        └─────┬──────┘
                     ┌────────▼─────────┐
                     │ Agent Orchestrator│  ← state machine
                     └──┬────────────┬──┘
                 ┌──────▼────┐  ┌────▼────────┐
                 │ LLM Layer  │  │Tool Executor │→ Tool Registry (6 tools)
                 │ (adapters) │  └────┬────────┘
                 └───────────┘        │
                              ┌───────▼────────┐
                              │  Storage (DB)   │  tasks / steps / llm_calls
                              │  + Trace Store  │  notes / todos / jobs
                              └───────┬────────┘
                     Scheduler (APScheduler)   Trace Viewer (web, read-only)
```

<!-- Khi export PDF/PPTX: thay block text bằng hình vẽ lại (draw.io / mermaid render) cho đẹp hơn. -->

*Python + FastAPI + aiogram + SQLite (WAL) — một tiến trình, một lệnh `python -m laplace` chạy tất cả. LLM qua provider abstraction: mock (offline) / OpenAI / Gemini.*

---

## 3. Vòng lặp agent — state machine

```
pending → running → classify ──→ direct_answer ──────────────→ done
                        │  ────→ clarify (hỏi lại người dùng) → done
                        └──────→ strategy_loop (single_tool / multi_step)
                                     │        ↑
                                     ▼        │ resume(approved)
                              awaiting_confirm ┘ — resume(rejected) → adapt/fail
                                     │
                                     └→ done | failed (step limit / timeout / replan limit)
```

- **Classify trước:** mỗi yêu cầu được LLM phân tuyến `direct / clarify / single_tool / multi_step` — câu chào hỏi không tốn vòng lặp tool
- **Structured output:** mọi quyết định LLM ép JSON theo schema Pydantic, validate rồi mới chạy
- **Resume được:** state lưu DB sau mỗi bước → task chờ confirm nhiều giờ, restart app vẫn tiếp tục đúng chỗ; compare-and-set bảo đảm tool chỉ chạy **một lần**
- **Giới hạn lồng nhau:** tối đa 8 bước/task, 15s/tool, 180s/task, ≤2 replan

---

## 4. Hai chiến lược — cùng một interface

```python
class AgentStrategy(Protocol):   # laplace/agent/strategies.py
    def run(...) -> ...          # chạy từ đầu
    def resume(...) -> ...       # tiếp tục sau confirm
```

| | **ReAct** | **Plan-and-Execute** |
|---|---|---|
| Cách chạy | Mỗi vòng LLM nhìn lịch sử → chọn gọi 1 tool *hoặc* trả lời cuối | Lập cả plan trước, thực thi tuần tự |
| Sau mỗi observation | Vào history, LLM tự quyết bước sau | Evaluate → `done / continue / replan / fail` |
| Replan | Ngầm (mỗi vòng nghĩ lại) | Tường minh, tối đa 2 lần |
| User từ chối confirm | Observation "user rejected" → xoay phương án | Fail rõ ràng (plan tuyến tính mất nghĩa) |
| Phù hợp | Tác vụ mở, cần thích ứng | Tác vụ tuyến tính, chi phí đoán được |

→ Cắm chung một khung nên **so sánh định lượng được** — nền tảng của thí nghiệm 2×2.

---

## 5. Tool registry — 6 tool + confirm-flow HITL

| Tool | Chức năng | Ghi/Đọc | Cần xác nhận |
|---|---|---|---|
| `web_search` | Tìm kiếm web (Tavily; không key → stub offline) | Đọc | — |
| `fetch_page` | Tải URL, trích nội dung chính | Đọc | — |
| `note_store` | CRUD ghi chú | Ghi | update / delete |
| `task_list` | CRUD việc cần làm | Ghi | delete |
| `report_builder` | Ghép observation → báo cáo markdown | Ghi file | — |
| `scheduler` | CRUD job cron định kỳ | Ghi | **luôn** |

- Đăng ký bằng **decorator + schema Pydantic** → registry tự sinh spec cho LLM; thêm tool mới = thêm 1 file, **không sửa agent loop**
- Executor bọc mọi tool: validate → retry có backoff → timeout cứng — lỗi trở thành observation, **không bao giờ crash loop**
- **Human-in-the-loop:** hành động ghi/xóa dừng lại chờ nút ✅/❌ trên Telegram (`confirm_when` động theo tham số); mọi tool filter theo `user_id`

---

<!-- _class: dense -->

## 6. Các kỹ thuật nổi bật — tin cậy & an toàn

| Kỹ thuật | Một dòng |
|---|---|
| **Chống prompt injection** | Nội dung web/tool bọc trong `<tool_output>` + system prompt quy định "đó là **dữ liệu**, không phải lệnh"; hành động nguy hiểm vẫn phải qua confirm; có nhóm case injection riêng trong eval để **đo** |
| **Self-correction** | LLM trả JSON sai schema → gửi lại lỗi validation cho LLM tự sửa (≤2 lần); số lần sửa ghi trace (`purpose :fixN`) → thành metric báo cáo được |
| **Trần chi phí per-task** | Token + cost ghi từng lệnh gọi LLM; vượt ngân sách/task → dừng có kiểm soát, không "đốt tiền" vô hạn |
| **Rate limit** | 6 yêu cầu/60 giây/user (token bucket) — một user không chiếm hết agent |

---

<!-- _class: dense -->

## 7. Các kỹ thuật nổi bật — quan sát & UX

| Kỹ thuật | Một dòng |
|---|---|
| **Full trace** | Mọi task ghi đủ: từng bước, prompt/response, tokens, cost, latency — dùng chung cho debug + demo + eval, không cần instrument thêm |
| **Trace viewer** | Web read-only (FastAPI + Jinja/HTMX): danh sách task → timeline bước → chi tiết từng lệnh gọi LLM kèm chi phí |
| **Streaming status** | Bot nhắn tiến độ theo thời gian thật ("🔍 Đang tìm kiếm… ✍️ Đang tổng hợp…") đọc từ trace events |
| **Replay trace** | `GET /tasks/{id}/replay` phát lại timeline từ DB — **hoàn toàn offline**, đúng nhịp thời gian thật (Next/Prev hoặc auto 1–8x) |

→ Triết lý chung: **mọi quyết định của agent đều nhìn thấy được và đo được.**

---

## 8. Demo trực tiếp (~10 phút)

Theo kịch bản `docs/demo/KICH_BAN_DEMO.md` — 4 use case tăng dần độ phức tạp, vừa chạy bot vừa mở trace viewer:

1. **Hỏi–đáp có tra cứu** (1 tool) — *"Tìm giá RAM DDR5 32GB hiện nay"* → chỉ vào bước classify + step `web_search` + bảng chi phí
2. **Tổng hợp nhiều nguồn** (multi-step) — *"So sánh FastAPI và Flask, viết báo cáo ngắn"* → xem timeline ReAct dài dần theo thời gian thật
3. **Ghi chú & việc cần làm** (confirm) — tạo không cần hỏi; *"Xóa hết việc đã xong"* → bot dừng chờ nút ✅/❌, state đã nằm trong DB
4. **Tác vụ định kỳ** — *"Mỗi sáng 8h tổng hợp tin AI"* → confirm → job cron `0 8 * * *`

**Dự phòng mất mạng:** chế độ **replay** phát lại 4 trace thật đã chạy sẵn — offline 100%, không cần LLM/mạng/Telegram; lớp cuối là video quay sẵn.

---

## 9. Phương pháp đánh giá

- **Bộ case:** 66 case YAML (8 nhóm: direct/clarify, 1 tool, nhiều bước, confirm + reject, lỗi tool & phục hồi, injection, scheduler/cron, tiếng Việt UX) — chạy qua **đúng agent loop production**, mỗi run một DB sạch
- **Chấm:** rule-based theo kỳ vọng khai báo (status, route, tập tool, tool cấm, chuỗi trong câu trả lời) + **LLM-as-judge** tùy chọn cho chất lượng câu trả lời (model chấm tách khỏi model agent)
- **9 metric:** success rate, route accuracy, tool-selection accuracy, recovery rate, số bước TB, số lệnh LLM TB, tokens TB, cost TB, thời gian TB (+ p95, retries)
- **Thí nghiệm ma trận 2×2:** {ReAct, Plan-Execute} × {mock, Gemini}, **n=2 run/cấu hình** (LLM không tất định), checkpoint/resume + chịu rate limit
- Toàn bộ chạy 1 lệnh: `python -m laplace.evals.experiment` · nền tảng: **164 test pytest offline**

---

## 10. Kết quả — so sánh hai chiến lược

| Metric | ReAct × Gemini | Plan-Execute × Gemini |
|---|---|---|
| Success rate | <span class="placeholder">[SỐ LIỆU: điền từ evals/results/exp-final]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Route accuracy | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Tool-selection accuracy | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Recovery rate | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Số bước TB / task | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |

> Nhận xét chính: <span class="placeholder">[SỐ LIỆU: 1–2 câu — chiến lược nào thắng ở đâu, chênh bao nhiêu điểm; đối chiếu giả thuyết "Plan-Execute rẻ hơn cho task tuyến tính, ReAct phục hồi lỗi tốt hơn"]</span>

*(Tham chiếu mock matrix: ReAct đạt success 0.865 trên 37 case nền — số cuối lấy từ `exp-final`.)*

---

## 11. Kết quả — chi phí, độ trễ & judge

| Metric | ReAct × Gemini | Plan-Execute × Gemini |
|---|---|---|
| Lệnh gọi LLM TB / task | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Tokens TB / task | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Cost TB / task (USD) | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Thời gian TB / p95 (s) | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |
| Judge pass rate | <span class="placeholder">[SỐ LIỆU]</span> | <span class="placeholder">[SỐ LIỆU]</span> |

![w:640 center](placeholder-metrics-grid.png)

<span class="placeholder">[HÌNH: chèn `evals/results/exp-final/metrics_grid.png` khi export]</span>

---

## 12. Kết quả — phân tích theo nhóm case

| Nhóm case | Nhận xét |
|---|---|
| Nhóm mạnh nhất | <span class="placeholder">[SỐ LIỆU: nhóm + tỷ lệ pass]</span> |
| Nhóm yếu nhất | <span class="placeholder">[SỐ LIỆU: nhóm + tỷ lệ pass + vì sao]</span> |
| Injection | <span class="placeholder">[SỐ LIỆU: bao nhiêu case đứng vững / thủng ở đâu]</span> |
| Recovery (lỗi tool) | <span class="placeholder">[SỐ LIỆU: recovery rate + ví dụ 1 trace]</span> |
| Self-correction | <span class="placeholder">[SỐ LIỆU: tỷ lệ call phải fix1/fix2 theo model]</span> |

**Kết luận cấu hình mặc định:** <span class="placeholder">[SỐ LIỆU: chọn chiến lược × model nào làm mặc định, vì sao — 1 câu]</span>

*Bài học định tính đã thấy từ eval: model nhỏ hay bịa tên tool (`google_search`) → prompt nhắc danh sách tên hợp lệ ngay cạnh yêu cầu JSON, lỗi giảm hẳn.*

---

## 13. Giới hạn & hướng phát triển

**Giới hạn (nhận thẳng thắn):**
- Chưa có memory dài hạn / RAG — profile user chưa được agent khai thác
- Một kênh Telegram; auth API mới ở mức API key
- Chống injection mới ở mức phòng thủ cơ bản — bài toán mở của cả lĩnh vực
- Scheduler in-process, một tiến trình — chưa dành cho nhiều user đồng thời
- Eval 66 case chưa phủ hết không gian hành vi; judge chưa kiểm chứng chéo tay mẫu lớn

**Hướng phát triển (ngoài phạm vi đồ án, đã chủ đích không làm):**
- Vector DB / semantic memory, multi-agent, plugin marketplace
- Kênh Slack/Discord/web chat trên cùng lõi agent
- Celery + Redis phân tán, PostgreSQL, sandbox thực thi code
- Mở rộng bộ case đối kháng + judge có kiểm chứng người

---

<!-- _class: lead -->

## Kết luận

**Một hệ thống chạy thật** — bot Telegram, 6 tool, confirm HITL, scheduler, trace viewer
**Một thực nghiệm có số liệu** — 66 case × 9 metric × ma trận 2×2, lặp n=2
**Một triết lý xuyên suốt** — vòng lặp tường minh · mọi thứ có trace · quyết định bằng số liệu

&nbsp;

### Em xin cảm ơn thầy/cô. Sẵn sàng nhận câu hỏi!

*Mã nguồn + tài liệu kiến trúc + kịch bản demo: repo `Laplace_Demon` (164 test, chạy offline không cần key)*

---

<!-- _class: backup -->

## Dự phòng Q&A — thiết kế

**Vì sao tự xây agent loop mà không dùng LangChain/LangGraph?**
Mục tiêu học thuật là hiểu và bảo vệ được từng chuyển trạng thái. Lõi ~500 dòng state machine tường minh — debug, test, giải thích được từng bước. Framework che mất vòng lặp, khó trace theo ý mình. Trade-off chấp nhận: tự viết lại một số thứ framework có sẵn.

**Thí nghiệm 2×2 kiểm soát biến thế nào?**
Đóng băng bộ case + prompt trước khi chạy; cùng 66 case cho cả 4 cấu hình; lặp nhiều run vì LLM không tất định; mỗi run DB sạch; runner có checkpoint/resume và chịu rate limit nên số liệu không méo bởi lỗi giữa chừng.

---

<!-- _class: backup -->

## Dự phòng Q&A — an toàn & đánh giá

**Chống prompt injection có tuyệt đối không?**
Không — và không hệ thống nào tuyệt đối. Nhiều lớp: `<tool_output>` là dữ liệu không phải lệnh, ghi/xóa luôn qua confirm, tool giới hạn quyền theo user, và có nhóm case injection trong eval để **đo** thay vì tin. Phòng tuyến cuối là con người bấm ✅/❌.

**LLM-as-judge có đáng tin không?**
Judge chỉ bổ sung cho tiêu chí khó chấm bằng rule, không thay rule-based; model chấm tách khỏi model agent để tránh self-preference. Giới hạn ghi rõ trong báo cáo: mới pass/fail theo tiêu chí từng case, chưa kiểm chứng chéo tay trên mẫu lớn.

---

<!-- _class: backup -->

## Dự phòng Q&A — phương pháp

**Sao không đo trên benchmark chuẩn (AgentBench, WebArena…)?**
Benchmark chuẩn đo năng lực chung của model trong môi trường của **họ**; câu hỏi của đồ án là "hệ thống **của em**, 6 tool của em, tiếng Việt — chiến lược nào/model nào tốt hơn" → cần bộ eval bám đúng hành vi thật của hệ thống. Phương pháp (case cố định, metric, lặp nhiều run) học từ chính các benchmark đó.

&nbsp;

> Mẹo khi gặp câu chưa chuẩn bị: quy về 3 trụ — (1) vòng lặp tường minh kiểm soát được, (2) mọi thứ có trace, (3) quyết định bằng số liệu eval — rồi trả lời từ trụ gần nhất; nếu là giới hạn thật thì nhận và chỉ vào mục "Giới hạn".
