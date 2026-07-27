# Kịch bản demo ngày bảo vệ

> Kịch bản từng bước cho phần demo trực tiếp (~10 phút) theo 4 use case trong [PLAN.md §2](../../PLAN.md), kèm **phương án mất mạng** bằng chế độ replay. Checklist quay video dự phòng: [VIDEO_DU_PHONG.md](VIDEO_DU_PHONG.md). Câu hỏi phản biện: [CAU_HOI_PHAN_BIEN.md](CAU_HOI_PHAN_BIEN.md).

## 0. Chuẩn bị trước khi lên bảng (làm ở nhà + kiểm tra lại 15 phút trước giờ)

- [ ] `.env`: `LAPLACE_LLM_PROVIDER=gemini` + key còn quota (kiểm tra bằng 1 task thử), `LAPLACE_TELEGRAM_BOT_TOKEN` đúng bot demo, `LAPLACE_SEARCH_API_KEY` (Tavily) còn hạn.
- [ ] Chạy `python -m laplace`, nhắn thử bot 1 câu → trả lời bình thường, **không có prefix `[mock]`**.
- [ ] Mở sẵn 2 cửa sổ: Telegram (chat với bot, phóng to chữ) và trình duyệt tab http://localhost:8010/ (trace viewer).
- [ ] **Đã chạy trước 4 use case ở nhà** và backup `laplace.db` → có trace sẵn cho phương án replay (mục 6). Ghi lại 4 task id vào tờ nháp/note.
- [ ] Video dự phòng nằm sẵn trên máy (không phụ thuộc mạng), đã thử mở.
- [ ] Tắt notification máy, bật Do Not Disturb; sạc pin; nếu được thì phát 4G từ điện thoại làm mạng dự phòng cho Telegram.
- [ ] Lưu ý rate limit của bot: 6 tin/60 giây/user — đừng gõ dồn dập khi demo.

**Câu mở đầu (30 giây):** "Em demo 4 kịch bản tăng dần độ phức tạp: hỏi–đáp có tra cứu, tổng hợp nhiều bước thành báo cáo, hành động ghi/xóa có xác nhận, và tác vụ định kỳ. Song song em sẽ mở trace viewer để cho thầy/cô thấy từng quyết định của agent — đây là điểm nhấn của đồ án: mọi thứ đều quan sát được."

## 1. Use case 1 — Hỏi–đáp có tra cứu (1 tool, ~1,5 phút)

| | |
|---|---|
| **Gõ vào bot** | `Tìm giúp tôi giá RAM DDR5 32GB hiện nay` |
| **Kỳ vọng** | Bot trả lời sau ~5–15s: tóm tắt mức giá kèm nguồn (từ Tavily). |
| **Điểm dừng giải thích** | Mở trace viewer → task mới nhất → trang chi tiết. Chỉ vào: (1) lệnh gọi LLM `classify` — "bước đầu tiên agent **phân loại**: đây là tuyến single_tool"; (2) step `web_search` với params + observation; (3) bảng llm_calls có token/cost/latency từng lệnh — "chi phí đo được đến từng call". |
| **Nói** | "Agent không hard-code luồng: LLM chọn tool từ registry, executor validate tham số bằng Pydantic rồi mới chạy." |

## 2. Use case 2 — Tổng hợp nhiều nguồn thành báo cáo (multi-step, ~3 phút)

| | |
|---|---|
| **Gõ vào bot** | `So sánh FastAPI và Flask cho backend đồ án, viết thành báo cáo ngắn` |
| **Kỳ vọng** | Chạy ~30–60s: search → fetch 1–3 trang → `report_builder` → bot trả tóm tắt + đường dẫn file markdown trong `reports/`. |
| **Trong lúc chờ** | Đây là điểm dừng kiến trúc chính (task đang chạy, trace viewer tự refresh 5s): mở trang chi tiết task, timeline dài dần theo thời gian thật. Chỉ vào từng step xuất hiện: "ReAct: mỗi vòng LLM nhìn lịch sử quan sát rồi quyết bước kế — không có kịch bản cứng". Chỉ vào cột route/strategy ở trang danh sách. |
| **Nói thêm** | "Nội dung web đưa về được đóng khung `<tool_output>` — là **dữ liệu**, không phải lệnh — đây là lớp chống prompt injection." Mở file báo cáo trong `reports/` cho xem kết quả cuối. |
| **Nếu 1 trang fetch lỗi** | Đừng chữa cháy — tận dụng luôn: chỉ vào step lỗi trong trace, "executor trả lỗi thành observation, agent tự đổi nguồn — đây là khả năng phục hồi" (use case 5 của PLAN xuất hiện tự nhiên). |

## 3. Use case 3 — Ghi chú & việc cần làm, có xác nhận (~2 phút)

| | |
|---|---|
| **Gõ (a)** | `Lưu ghi chú: deadline proposal 30/8` |
| **Kỳ vọng (a)** | Tạo ghi chú **không cần confirm** (create là hành động an toàn) → bot báo đã lưu. |
| **Gõ (b)** | `Xóa hết việc đã xong trong danh sách việc của tôi` |
| **Kỳ vọng (b)** | Bot **dừng lại hỏi** với nút ✅ Đồng ý / ❌ Từ chối. |
| **Điểm dừng giải thích** | TRƯỚC khi bấm nút: mở trace viewer → task đang `awaiting_confirm`, step `pending_confirm`. Nói: "Toàn bộ state vòng lặp đã lưu xuống DB — restart app vẫn resume được; hai người bấm nút cùng lúc thì compare-and-set bảo đảm tool chỉ chạy một lần." |
| **Kết** | Bấm ✅ → tool chạy, bot báo kết quả. (Nếu còn giờ: lặp lại và bấm ❌ để cho thấy nhánh từ chối — ReAct nhận observation "user rejected" và xoay phương án.) |

## 4. Use case 4 — Tác vụ định kỳ (~1,5 phút)

| | |
|---|---|
| **Gõ vào bot** | `Mỗi sáng 8h tổng hợp tin AI mới nhất và gửi cho tôi` |
| **Kỳ vọng** | Tool `scheduler` **luôn cần confirm** → nút ✅/❌ → bấm ✅ → bot báo đã tạo job cron `0 8 * * *`. |
| **Điểm dừng giải thích** | "Job nằm trong bảng `scheduled_jobs`, APScheduler tự nạp lại ngay khi tool tạo xong; đến giờ, scheduler tạo task chạy đúng agent loop này rồi đẩy kết quả về Telegram." Không chờ được 8h sáng — chỉ vào trace của một job đã chạy sẵn ở nhà (hoặc nói tham chiếu video dự phòng). |

## 5. Chốt demo (~1 phút)

Quay lại trang danh sách trace viewer: "4 task vừa rồi — mỗi task đủ timeline, token, chi phí, độ trễ. Cùng nguồn dữ liệu này em xây eval harness 66 case chạy tự động để so sánh 2 chiến lược × 2 model — số liệu trong báo cáo." → chuyển sang phần Q&A.

## 6. Phương án MẤT MẠNG — chế độ replay (chuẩn bị sẵn, chuyển trong 30 giây)

Chế độ replay phát lại timeline từ trace **đã lưu trong DB, hoàn toàn offline** — không cần mạng, không cần LLM, không cần Telegram (route `GET /tasks/{id}/replay`, server-render thuần, không cần cả JavaScript).

### Chuẩn bị ở nhà (bắt buộc, một lần)

1. Chạy thật 4 use case ở trên qua Telegram (LLM thật) → DB có 4 trace đẹp. Chạy lại nếu trace xấu (lỗi giữa chừng, câu trả lời dở).
2. **Dừng app** (Ctrl+C — để WAL checkpoint), rồi backup: `cp laplace.db laplace.db.demo-backup` (nếu còn thì kèm `laplace.db-wal`, `laplace.db-shm`).
3. Ghi 4 task id (xem ở trang danh sách trace viewer) vào tờ nháp, ví dụ: UC1=12, UC2=13, UC3=14, UC4=15.
4. Diễn tập một lần: khôi phục backup vào máy demo, tắt WiFi, chạy `python -m laplace` (mock/không key vẫn được — replay không gọi LLM), mở đủ 4 URL replay.

### Khi demo bị mất mạng

1. Nói thẳng: "Mất mạng là rủi ro em đã dự phòng — hệ thống có chế độ replay phát lại trace thật đã chạy, đúng dữ liệu, đúng nhịp thời gian."
2. Nếu app chưa chạy: khôi phục DB backup (`cp laplace.db.demo-backup laplace.db`) rồi `python -m laplace` (không cần key).
3. Mở `http://localhost:8010/tasks/<id>/replay` cho từng use case:
   - **Kể chuyện từng bước**: bấm **Next/Prev** (`?upto=N` tăng dần) — dừng ở đúng các điểm giải thích của mục 1–4.
   - **Chạy như thật**: thêm `?auto=1&speed=2` — tự phát theo latency thật (tốc độ 1x/2x/4x/8x), phù hợp khi muốn vừa chạy vừa nói.
4. Nội dung thuyết minh giữ nguyên như mục 1–4 (điểm dừng, câu nói) — chỉ khác là nhìn timeline phát lại thay vì chờ bot chạy thật.
5. Nếu cả máy demo trục trặc → video dự phòng ([VIDEO_DU_PHONG.md](VIDEO_DU_PHONG.md)).

## 7. Bảng thời lượng

| Phần | Thời lượng |
|---|---|
| Mở đầu + giới thiệu màn hình | 0:30 |
| UC1 — tra cứu 1 bước + trace | 1:30 |
| UC2 — multi-step + báo cáo + giải thích kiến trúc | 3:00 |
| UC3 — confirm (approve/reject) | 2:00 |
| UC4 — scheduler | 1:30 |
| Chốt + chuyển Q&A | 1:00 |
| **Tổng** | **~9:30** (dư ~30s đệm) |
