# 20 câu hỏi phản biện dự kiến + gợi ý trả lời

> Luyện trước ngày bảo vệ (T19). Trả lời ngắn 20–40 giây/câu; câu nào sâu hơn thì mở trace viewer / tài liệu [KIEN_TRUC.md](../KIEN_TRUC.md) minh họa. Nguyên tắc chung: thừa nhận giới hạn thẳng thắn + chỉ ra mình đã lường trước (có trong PLAN/báo cáo) tốt hơn là chống chế.

## Nhóm thiết kế & kiến trúc

**1. Vì sao tự xây agent loop mà không dùng LangChain/LangGraph?**
Mục tiêu học thuật là hiểu và bảo vệ được từng chuyển trạng thái. Lõi ~500 dòng, là state machine tường minh — debug, test và giải thích được. Framework che mất vòng lặp, khó trace theo ý mình và khó trả lời "vì sao agent làm bước này". Trade-off chấp nhận: tự viết lại một số thứ framework có sẵn.

**2. ReAct và Plan-and-Execute khác nhau thế nào, khi nào dùng cái nào?**
ReAct: mỗi vòng LLM nhìn toàn bộ lịch sử rồi chọn gọi tool hay trả lời — thích ứng tốt với tác vụ mở, chi phí khó đoán. Plan-Execute: lập kế hoạch trước, thực thi tuần tự, sau mỗi bước evaluate `done/continue/replan/fail` (tối đa 2 replan) — chi phí đoán được, cứng hơn khi môi trường đổi. Cả hai cắm chung interface `AgentStrategy {run, resume}` nên so sánh định lượng được — số liệu trong phần thực nghiệm.

**3. Vì sao chọn Telegram làm kênh giao tiếp?**
Bot API miễn phí, long polling không cần public IP/webhook, có sẵn nút bấm (inline keyboard) rất hợp luồng confirm, và là kênh cá nhân dùng hằng ngày — đúng chất "agent cá nhân". Lõi agent tách khỏi kênh: thêm web chat/Slack chỉ là thêm một mặt tiền.

**4. Vì sao dùng SQLite mà không phải PostgreSQL?**
Quy mô cá nhân, một máy, một tiến trình — SQLite (bật WAL + busy_timeout) là zero-config và đủ. Đường lên PostgreSQL đã mở sẵn qua `LAPLACE_DB_URL` (SQLAlchemy). Đây là quyết định phạm vi, không phải giới hạn kiến trúc.

**5. Kiến trúc một tiến trình (bot + web + scheduler) có phải điểm yếu?**
Với MVP cá nhân là điểm mạnh: đơn giản, dễ vận hành, một lệnh chạy tất cả. Điểm yếu khi scale nhiều user: agent loop sync chiếm 1 thread/task. Hướng đi đã ghi trong báo cáo: tách worker + queue, PostgreSQL, scheduler phân tán.

**6. Trace đầy đủ để làm gì, có đắt không?**
Ba việc: (1) debug — dựng lại "agent vừa làm gì, vì sao"; (2) demo — trace viewer + chế độ replay offline; (3) đánh giá — eval harness chấm điểm trực tiếp từ trace, không cần instrument thêm. Chi phí ghi DB không đáng kể so với latency gọi LLM.

**7. State lưu trong DB thay vì RAM — vì sao?**
Pause/resume sống qua restart: task đang chờ confirm có thể được duyệt nhiều giờ sau, thậm chí ở tiến trình khác. Kèm compare-and-set khi resume để hai request confirm đua nhau thì tool cũng chỉ chạy đúng một lần.

## Nhóm an toàn

**8. Chống prompt injection thế nào? Có tuyệt đối không?**
Nhiều lớp: nội dung tool/web bị đóng khung `<tool_output>` và system prompt quy định đó là dữ liệu, không phải lệnh; hành động ghi/xóa luôn qua confirm của người dùng; tool bị giới hạn quyền theo user; có nhóm case injection trong bộ eval để đo. Không tuyệt đối — đây là bài toán mở của cả lĩnh vực; phòng tuyến cuối là con người bấm ✅/❌.

**9. Điều gì ngăn agent chạy mãi hoặc phá dữ liệu?**
Giới hạn lồng nhau: tối đa 8 bước/task, timeout 15s/tool, 180s/task, tối đa 2 replan, retry tool có hạn; hành động ghi/xóa cần confirm; mọi tool đọc/ghi DB lọc theo `user_id`. Tất cả có test.

**10. Một user spam thì sao?**
Rate limit theo Telegram user: 6 yêu cầu/60 giây, quá thì bot báo chờ — một user không chiếm hết agent loop. Đầu vào cũng chặn tin nhắn quá 2000 ký tự.

**11. Nếu LLM trả về JSON sai schema thì sao?**
Mọi quyết định đều ép JSON theo schema Pydantic; sai thì thông báo lỗi validation được gửi lại cho LLM tự sửa (tối đa 2 lần), vẫn sai thì task fail với lỗi rõ ràng. Số lần tự sửa được ghi trace (purpose `:fixN`) — thành một metric trong báo cáo. Bài học thực tế: model nhỏ hay bịa tên tool (`google_search`) → prompt nhắc danh sách tên hợp lệ ngay cạnh yêu cầu JSON, lỗi giảm hẳn.

**12. Tool bị treo/lỗi mạng thì agent có sập không?**
Không — executor bọc mọi tool: validate tham số, retry có backoff, timeout cứng trong worker thread, và không bao giờ để exception lọt ra loop; lỗi trở thành observation để LLM đổi hướng. Giới hạn thật (có trong báo cáo): Python không kill được thread đang chạy — thread quá hạn bị bỏ lại có kiểm soát.

## Nhóm đánh giá & thực nghiệm

**13. Đánh giá agent bằng cách nào? Vì sao tin được số liệu?**
Eval harness chạy 66 case YAML (8 nhóm, gồm cả injection và phục hồi lỗi) qua **đúng agent loop production**, mỗi run một DB sạch, chấm rule-based theo kỳ vọng khai báo (status, route, tập tool, tool cấm, chuỗi trong câu trả lời) → 9 metric. LLM không tất định nên mỗi cấu hình chạy n=3 lượt và báo cáo trung bình (exp-final: 462 run tổng).

**14. LLM-as-judge có đáng tin không? Thiên vị thì sao?**
Judge chỉ dùng cho tiêu chí chất lượng khó chấm bằng rule, là tùy chọn bổ sung chứ không thay rule-based. Thiết kế cho phép tách model chấm khỏi model agent (tránh self-preference); giới hạn thực tế ghi thẳng trong báo cáo: lần chạy mẫu judge dùng chính `gemini-3.1-flash-lite` vì đó là model thật duy nhất khả dụng, và judge mới pass/fail theo tiêu chí từng case, chưa kiểm chứng chéo tay trên mẫu lớn. Đáng chú ý: judge vẫn chấm **fail** chính model đó ở 6/9 run (scheduler bị từ chối, recovery không đổi nguồn) — không thấy dấu hiệu nương tay.

**15. Thí nghiệm kiểm soát biến thế nào? Vì sao không đủ ma trận 2×2?**
Đóng băng bộ case + prompt trước khi chạy; n=3 run/case; mỗi run DB sạch; runner có checkpoint/resume và chịu rate limit nên số liệu không bị méo bởi lỗi giữa chừng. Thực tế: phần mock chạy **đầy đủ** 2 chiến lược × 66 case × n=3 = 396 run; phần Gemini kế hoạch chạy đủ ma trận nhưng free tier (500 req/ngày) cạn quota nên thu hẹp còn **mẫu minh họa** ReAct × 22 case × n=3 = 66 run — ghi trung thực thành giới hạn trong báo cáo, so sánh chéo model đầy đủ là future work.

**16. Kết quả chính của thực nghiệm là gì?**
Ba con số đinh (nguồn: `evals/results/exp-final/`): (1) trên môi trường mock, khung agent đạt **100% ở mọi run trùng chiến lược gốc** — ReAct 171/171, Plan-Execute 27/27 — chứng minh khung chạy đúng cả 2 chiến lược; (2) trên model thật (mẫu Gemini `gemini-3.1-flash-lite` × ReAct, 22 case × n=3), success rate **72,7%** (48/66 run), route accuracy **94,4%**, tool-selection 82,5%; (3) nhóm yếu nhất là **nhiều bước (52,4%)** và **phục hồi lỗi (0/3)** — model thừa nhận lỗi fetch nhưng không tự đổi sang tìm kiếm; nhóm mạnh nhất là direct/clarify và **injection: không run nào bị vượt** (6/6 Gemini + 30/30 mock). Từ đó giữ ReAct làm chiến lược mặc định. Lưu ý trung thực: mock là môi trường kịch bản nên chỉ so được success/route/tool, không so cost/latency; so sánh 2 chiến lược trên model thật chưa làm được vì quota — là future work.

**17. Sao không đo trên benchmark chuẩn (AgentBench, WebArena…)?**
Benchmark chuẩn đo năng lực chung của model trong môi trường của họ; câu hỏi của đồ án là "hệ thống **của em**, tool của em, tiếng Việt, chiến lược nào/model nào tốt hơn" — cần bộ eval bám đúng 6 tool và hành vi thật của hệ thống. Phương pháp (case cố định, metric, lặp nhiều run) học từ các benchmark đó.

## Nhóm vận hành & giới hạn

**18. Chi phí chạy thật khoảng bao nhiêu?**
Token + cost ghi từng lệnh gọi LLM trong trace nên trả lời được bằng số đo, không ước lượng. Số từ mẫu Gemini exp-final (66 run): trung bình **~3.800 token/task** (prompt ~3.400 + completion ~400), chi phí ước tính **~$0,0005/task** — cả 66 run hết **~$0,033**. Độ trễ trung vị ~5s/task; trung bình 25,7s vì bị kéo bởi backoff khi dính 429 của free tier (11/66 run >60s). Free tier Gemini (500 req/ngày) đủ cho demo nhưng là nút thắt của eval — chính vì vậy phần model thật chỉ là mẫu 22 case.

**19. Mất mạng / API sập ngay lúc demo thì sao?**
Đã dự phòng 2 lớp: chế độ **replay** phát lại trace thật từ DB, hoàn toàn offline, đúng nhịp thời gian (đang demo được ngay); và video quay sẵn. Ngoài demo, hệ thống chạy được không cần key nào nhờ mock LLM + stub search — toàn bộ test và eval mock chạy offline.

**20. Giới hạn lớn nhất của hệ thống và hướng phát triển?**
Trung thực theo báo cáo: chưa có memory dài hạn/RAG (profile user chưa được agent dùng), một kênh Telegram, auth API mới ở mức API key, injection mới phòng thủ cơ bản, scheduler in-process, eval 66 case chưa phủ hết không gian hành vi. Ưu tiên tiếp theo: memory có cấu trúc + RAG, thêm kênh trên cùng lõi, mở rộng bộ case đối kháng và judge có kiểm chứng người.

---

**Mẹo khi bị hỏi câu không chuẩn bị:** quy về 3 trụ của đồ án — (1) vòng lặp tường minh kiểm soát được, (2) mọi thứ có trace, (3) quyết định bằng số liệu eval — rồi trả lời từ trụ gần nhất; nếu là giới hạn thật thì nhận và chỉ vào mục "Giới hạn" của báo cáo.
