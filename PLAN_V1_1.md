# Plan v1.1 — sau khi tag v1.0 (lập 27/07)

> v1.0 đã chốt: agent 2 chiến lược, 6 tool, confirm HITL, trace viewer + replay,
> streaming, cost tracking + trần chi phí, 66 eval case, báo cáo 19 trang + slide,
> multi-provider đợt 1 (8 preset + wizard). Đang chạy: T16 (trang /settings web).
>
> Nguyên tắc chặng này: **ưu tiên những gì phục vụ bảo vệ đồ án trước**, tính năng sau.

## Nhánh A — Hoàn tất đồ án (ưu tiên cao nhất, phần lớn là việc của NGƯỜI)

| # | Việc | Ai | Ước | Ghi chú |
|---|---|---|---|---|
| A1 | Demo 4 kịch bản × 3 lần trên Telegram thật | **bạn** | 1 buổi | Tiêu chí DoD cuối chưa tick; bot đang chạy sẵn; vừa demo vừa để dành trace đẹp cho replay |
| A2 | Backup `laplace.db` chứa trace demo đẹp | tôi | 5' | Ngay sau A1 — nguyên liệu cho replay ngày bảo vệ + video |
| A3 | Render slide PDF/PPTX (`marp-cli`) + đọc soát | bạn | 1h | `docs/slides/README.md` có lệnh sẵn |
| A4 | Quay video dự phòng theo checklist 7 cảnh | bạn | 1 buổi | `docs/demo/VIDEO_DU_PHONG.md`; quay SAU A1 để có trace thật |
| A5 | Đọc soát báo cáo 19 trang, sửa giọng | bạn (+tôi sửa) | 1-2h | Đặc biệt chương 6 (diễn giải 86,4/21,2) và chương 7 (giới hạn) |

## Nhánh B — Trả nợ số liệu model thật (làm được ngay khi B1 xong, KHÔNG chặn nhánh A)

| # | Việc | Ai | Ước | Ghi chú |
|---|---|---|---|---|
| B1 | Lấy key Groq (free, console.groq.com/keys) dán qua wizard/settings | **bạn** | 5' | Groq free tier thoáng + nhanh hơn Gemini free nhiều |
| B2 | Chạy lại ma trận: `--providers groq,mock --strategies react,plan_execute --runs 3 --judge groq` | tôi | 1-2h máy | Runner có sẵn checkpoint/resume; tên exp mới `exp-groq` |
| B3 | Cập nhật báo cáo/slide: thay "mẫu Gemini 22 case" bằng bảng model thật đầy đủ | tôi (agent) | 1h | Trả món nợ "so sánh model" trong future work → thành kết quả thật |

## Nhánh C — Providers đợt 2 (tính năng, sau A)

| # | Việc | Ước | Ghi chú |
|---|---|---|---|
| C1 | Merge T16 (trang /settings) khi agent xong | 10' | đang chạy |
| C2 | Failover: `LAPLACE_LLM_FALLBACK=groq,ollama` — 429/hết quota N lần → tự chuyển hãng | 0.5 ngày | bài học đêm 26/07; wrap ở get_provider, không đụng agent |
| C3 | Lệnh Telegram `/model` (admin): xem/đổi provider+model runtime | 0.5 ngày | không nhận key qua chat |
| C4 | Anthropic native adapter | 0.5 ngày | CHỈ nếu cần gọi Claude thẳng; OpenRouter đã cover |

## Nhánh D — Vận hành (tùy chọn, sau cùng)

- D1. `LAPLACE_WEB_HOST` config: mặc định `127.0.0.1` (an toàn), ngày demo đổi `0.0.0.0` — 30'.
- D2. Deploy VPS (T17 PLAN.md): docker compose đã sẵn, chỉ cần VPS + .env — 1 buổi.
- D3. CI GitHub Actions: pytest + ruff mỗi push — 30', repo đã public trên GitHub.

## Thứ tự đề xuất trong tuần này

1. **C1** (merge T16) → 2. **B1+B2** (key Groq + chạy đêm) → 3. **A1** demo thật (song song B2)
→ 4. **A2, B3** → 5. **A3–A5** chuẩn bị bảo vệ → 6. C2–C3 nếu còn hứng → tag `v1.1`.

Việc chỉ bạn làm được: A1, A3, A4, A5, B1. Còn lại tôi/agent tự chạy.
