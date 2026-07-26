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
Eval harness chạy 66 case YAML (8 nhóm, gồm cả injection và phục hồi lỗi) qua **đúng agent loop production**, mỗi run một DB sạch, chấm rule-based theo kỳ vọng khai báo (status, route, tập tool, tool cấm, chuỗi trong câu trả lời) → 9 metric. LLM không tất định nên mỗi cấu hình chạy ≥3 lần và báo cáo trung bình.

**14. LLM-as-judge có đáng tin không? Thiên vị thì sao?**
Judge chỉ dùng cho tiêu chí chất lượng khó chấm bằng rule, là tùy chọn bổ sung chứ không thay rule-based. Chống thiên vị: model chấm tách khỏi model agent (tránh self-preference). Giới hạn ghi rõ trong báo cáo: mới pass/fail theo tiêu chí từng case, chưa kiểm chứng chéo tay trên mẫu lớn.

**15. Thí nghiệm 2×2 kiểm soát biến thế nào?**
Đóng băng bộ case + prompt trước khi chạy; cùng 66 case cho cả 4 cấu hình {ReAct, Plan-Execute} × {2 model}; ≥3 run/case; mỗi run DB sạch; runner có checkpoint/resume và chịu rate limit nên số liệu không bị méo bởi lỗi giữa chừng.

**16. Kết quả chính của thực nghiệm là gì?**
*(Điền số thật từ `evals/results/` trước ngày bảo vệ — nhớ 3–4 con số đinh: success rate từng chiến lược, chênh lệch cost/latency, recovery rate, nhóm case yếu nhất.)* Cấu trúc trả lời: "Chiến lược X thành công cao hơn Y điểm nhưng tốn Z lần token; nhóm case khó nhất là …; từ đó em chọn cấu hình mặc định là …".

**17. Sao không đo trên benchmark chuẩn (AgentBench, WebArena…)?**
Benchmark chuẩn đo năng lực chung của model trong môi trường của họ; câu hỏi của đồ án là "hệ thống **của em**, tool của em, tiếng Việt, chiến lược nào/model nào tốt hơn" — cần bộ eval bám đúng 6 tool và hành vi thật của hệ thống. Phương pháp (case cố định, metric, lặp nhiều run) học từ các benchmark đó.

## Nhóm vận hành & giới hạn

**18. Chi phí chạy thật khoảng bao nhiêu?**
Token + cost ghi từng lệnh gọi LLM trong trace nên trả lời được bằng số đo, không ước lượng. *(Xem trước một con số trung bình/task từ trace hoặc eval results để nói ngay; free tier Gemini đủ cho demo/eval, có retry 429 theo gợi ý của API.)*

**19. Mất mạng / API sập ngay lúc demo thì sao?**
Đã dự phòng 2 lớp: chế độ **replay** phát lại trace thật từ DB, hoàn toàn offline, đúng nhịp thời gian (đang demo được ngay); và video quay sẵn. Ngoài demo, hệ thống chạy được không cần key nào nhờ mock LLM + stub search — toàn bộ test và eval mock chạy offline.

**20. Giới hạn lớn nhất của hệ thống và hướng phát triển?**
Trung thực theo báo cáo: chưa có memory dài hạn/RAG (profile user chưa được agent dùng), một kênh Telegram, auth API mới ở mức API key, injection mới phòng thủ cơ bản, scheduler in-process, eval 66 case chưa phủ hết không gian hành vi. Ưu tiên tiếp theo: memory có cấu trúc + RAG, thêm kênh trên cùng lõi, mở rộng bộ case đối kháng và judge có kiểm chứng người.

---

**Mẹo khi bị hỏi câu không chuẩn bị:** quy về 3 trụ của đồ án — (1) vòng lặp tường minh kiểm soát được, (2) mọi thứ có trace, (3) quyết định bằng số liệu eval — rồi trả lời từ trụ gần nhất; nếu là giới hạn thật thì nhận và chỉ vào mục "Giới hạn" của báo cáo.
