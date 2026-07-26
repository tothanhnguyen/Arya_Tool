# Checklist quay video demo dự phòng (T19)

> Video chiếu khi demo trực tiếp KHÔNG thể chạy (hỏng máy, mất mạng và replay cũng trục trặc). Quay theo đúng kịch bản [KICH_BAN_DEMO.md](KICH_BAN_DEMO.md) để lời thuyết minh dùng chung được cho cả hai phương án.

## Nguyên tắc

- **Một lần quay ăn ngay khó** — quay từng cảnh rời rồi ghép; mỗi cảnh quay lại được độc lập.
- Video **không lồng tiếng** (thuyết minh trực tiếp khi chiếu) — nhưng chèn phụ đề/chú thích ngắn ở góc màn hình cho từng bước, phòng khi bị hỏi lại chi tiết.
- Tổng độ dài mục tiêu: **5–6 phút** (ngắn hơn demo sống vì cắt được thời gian chờ).

## Chuẩn bị trước khi quay

- [ ] Máy sạch notification, Do Not Disturb, đồng hồ hệ thống hiện giờ hợp lý.
- [ ] Font/zoom: Telegram và trình duyệt phóng to ≥125% — chữ đọc được khi chiếu máy chiếu.
- [ ] `.env` dùng LLM thật (Gemini/OpenAI) + Tavily key; kiểm tra không còn prefix `[mock]`.
- [ ] DB sạch (xóa `laplace.db*` cũ) để trang danh sách trace không lộn xộn.
- [ ] Bố cục màn hình cố định: Telegram bên trái, trace viewer bên phải (hoặc chuyển tab dứt khoát) — giữ nguyên suốt các cảnh.

## Danh sách cảnh quay (theo thứ tự ghép)

| # | Cảnh | Nội dung quay | Độ dài mục tiêu |
|---|---|---|---|
| 1 | Mở màn | `python -m laplace` khởi động (log lên đủ web + bot + scheduler), mở trace viewer trống, `/start` với bot | 0:20 |
| 2 | UC1 — tra cứu | Gõ câu UC1 → bot trả lời kèm nguồn; cắt sang trace: classify → web_search → final; lia qua cột token/cost | 0:50 |
| 3 | UC2 — multi-step | Gõ câu UC2; tua nhanh (speed-up 2–4x đoạn chờ) timeline trace dài dần; mở file báo cáo trong `reports/` | 1:20 |
| 4 | UC3 — confirm | Ghi chú (không hỏi) → xóa việc đã xong (nút ✅/❌ hiện) → **dừng hình** ở trace `awaiting_confirm`/`pending_confirm` → bấm ✅ → kết quả. Quay thêm nhánh bấm ❌ làm cảnh phụ (4b, có thể cắt nếu dài) | 1:00 (+0:30) |
| 5 | UC4 — scheduler | Gõ câu UC4 → confirm → bot báo job `0 8 * * *`; chỉ vào job trong trace/DB; cắt cảnh kết quả job đã chạy (chuẩn bị sẵn từ hôm trước) đẩy về Telegram | 0:50 |
| 6 | Phục hồi lỗi (điểm cộng) | Trace một task có step lỗi (fetch fail) → agent đổi nguồn → vẫn done; chú thích "tool lỗi thành observation, không crash" | 0:30 |
| 7 | Replay + eval (chốt) | Mở `/tasks/{id}/replay?auto=1&speed=4` chạy tự động vài giây ("demo offline được"); lướt `report.md` của eval harness (bảng metric) | 0:40 |

## Công cụ

- **Quay màn hình**: QuickTime Player (macOS, File → New Screen Recording) hoặc OBS nếu cần quay cả webcam. Quay cả màn hình, 1080p trở lên.
- **Cắt ghép + tua nhanh + chú thích**: iMovie / CapCut / DaVinci Resolve (miễn phí đều đủ). ffmpeg cho thao tác nhanh: cắt `ffmpeg -ss 00:00:05 -to 00:01:00 -i in.mov -c copy out.mp4`, tua nhanh 4x `ffmpeg -i in.mp4 -vf "setpts=PTS/4" -an out.mp4`.
- **Xuất**: MP4 H.264 1080p, một file duy nhất `demo_du_phong.mp4`.

## Sau khi dựng xong

- [ ] Xem lại từ đầu đến cuối MỘT lần ở đúng máy sẽ mang đi bảo vệ (codec/player ổn).
- [ ] Chép ra ≥2 nơi: máy demo + USB (+ điện thoại/cloud nếu được).
- [ ] Ghi chú timestamp từng cảnh vào tờ nháp để khi hội đồng hỏi "cho xem lại đoạn X" thì tua đúng chỗ ngay.
- [ ] Diễn tập thuyết minh theo video 1 lần, bấm giờ.
