# Slide bảo vệ đồ án

`slides.md` là bộ slide Marp (~15 slide chính + 3 slide dự phòng Q&A) cho phần trình bày 10 phút.

## Render

```bash
# Cài marp-cli (một lần)
npm i -g @marp-team/marp-cli

# Xem trực tiếp khi sửa
marp -p docs/slides/slides.md

# Xuất PDF / PPTX
marp docs/slides/slides.md --pdf  -o docs/slides/slides.pdf
marp docs/slides/slides.md --pptx -o docs/slides/slides.pptx
```

## Việc còn lại trước ngày bảo vệ

1. **Điền số liệu**: tìm các chỗ đánh dấu `[SỐ LIỆU: ...]` (slide 10–12) và thay bằng số thật từ `evals/results/exp-final/` (`report.md` + `results.json`).
2. **Chèn biểu đồ**: slide 11 tham chiếu `placeholder-metrics-grid.png` — copy `evals/results/exp-final/metrics_grid.png` vào `docs/slides/` và đổi tên/đường dẫn trong slide.
3. **Sơ đồ kiến trúc** (slide 2): đang là block text — nếu muốn đẹp hơn khi export, vẽ lại bằng draw.io/mermaid rồi thay bằng hình.
4. Ngày trình bày: cập nhật thời gian trên slide bìa nếu khác 12/2026.

Nội dung khớp với: `PLAN.md`, `docs/KIEN_TRUC.md`, `docs/demo/KICH_BAN_DEMO.md`, `docs/demo/CAU_HOI_PHAN_BIEN.md`.
