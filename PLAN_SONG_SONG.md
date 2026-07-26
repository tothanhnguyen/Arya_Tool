# Plan làm việc song song — nhiều terminal Claude Code

> Mục tiêu: hoàn thành giai đoạn 4 (đánh giá & tối ưu) của `PLAN.md` nhanh hơn bằng 4 terminal chạy song song, mỗi terminal một git worktree riêng để không giẫm chân nhau.

## Bước 0 — Chuẩn bị (terminal chính, làm TRƯỚC, ~5 phút)

Main đang có 13 file sửa dở. Phải commit trước để các worktree tách ra từ trạng thái sạch:

```bash
cd ~/Documents/Laplace_Demon
git add -A && git commit -m "feat(evals): cai tien harness + case 07, scheduler, docs"

# Tạo 4 worktree (thư mục nằm cạnh repo chính)
git worktree add ../laplace-evals   -b feat/eval-cases-40
git worktree add ../laplace-exp     -b feat/experiment-2x2
git worktree add ../laplace-harden  -b feat/hardening
git worktree add ../laplace-docs    -b chore/deploy-docs
```

Sau đó mở 4 cửa terminal, mỗi cửa `cd` vào một thư mục trên và chạy `claude`.

## Phân công 4 terminal

### Terminal 1 — `../laplace-evals` — Mở rộng bộ eval 13 → 40 case ⭐ dùng nhiều agent

Mỗi case là 1 file YAML riêng trong `evals/cases/` → các agent viết song song không đụng nhau. Đây là terminal nên tận dụng đa agent.

**Quy ước:** terminal này CHỈ thêm file YAML mới + test đếm case, KHÔNG sửa `laplace/evals/harness.py` (thuộc Terminal 2).

Prompt gợi ý dán vào Claude:

> Đọc `laplace/evals/harness.py` và các case mẫu trong `evals/cases/` để hiểu format. Sau đó spawn 6 agent song song, mỗi agent viết 4–5 case YAML mới cho một nhóm: (1) direct/clarify, (2) single-tool, (3) multi-step, (4) confirm-flow, (5) prompt injection, (6) tool error/recovery. Đánh số file tiếp từ 12 trở đi, không trùng tên. Xong thì chạy eval với provider mock để chắc mọi case parse được, rồi commit.

### Terminal 2 — `../laplace-exp` — Thí nghiệm 2×2 + báo cáo metric

Chạy ma trận {ReAct, Plan-Execute} × {2 model}, ≥3 run/cấu hình, xuất bảng metric + biểu đồ vào `evals/results/`. Job này chạy lâu (rate limit Gemini) → để riêng một terminal là đúng bài.

**Sở hữu file:** `laplace/evals/harness.py`, `laplace/evals/__main__.py`, `evals/results/`.

Prompt gợi ý:

> Viết runner thí nghiệm 2×2: chiến lược {react, plan_execute} × model (Gemini + mock hoặc model thứ 2 nếu có key), mỗi cấu hình ≥3 run trên toàn bộ case trong `evals/cases/`. Lưu kết quả thô JSON + bảng tổng hợp 8 metric (success rate, tool accuracy, số bước, latency, cost, recovery...) vào `evals/results/`, sinh biểu đồ so sánh. Chịu được rate limit (retry/backoff, resume được giữa chừng). Chạy thử trước với mock, commit, rồi chạy thật với Gemini.

Phụ thuộc: chạy được ngay với 13 case hiện có; sau khi merge Terminal 1 thì chạy lại trên 40 case để lấy số liệu cuối.

### Terminal 3 — `../laplace-harden` — Hardening + tối ưu prompt

**Sở hữu file:** `laplace/agent/prompts.py`, `laplace/tools/*`, `laplace/agent/orchestrator.py`, `tests/*`.

Prompt gợi ý:

> Hardening theo T16 của PLAN.md: (1) rà soát chống prompt injection trong nội dung fetch từ web (delimiter + system prompt), viết test; (2) error taxonomy + retry/backoff cho tool executor, test lỗi mạng/timeout; (3) rate limit theo user; (4) tối ưu tool description + prompt trong `laplace/agent/prompts.py` cho rõ ràng hơn. Có thể spawn agent song song cho từng mục vì khác file. Chạy đủ `pytest` trước khi commit.

### Terminal 4 — `../laplace-docs` — Deploy + tài liệu

**Sở hữu file:** `Dockerfile`, `docker-compose.yml`, `README.md`, `docs/*`.

Prompt gợi ý:

> Theo T17 của PLAN.md: kiểm tra và hoàn thiện Dockerfile + docker-compose (build được từ máy sạch, healthcheck, volume cho SQLite), viết README cài đặt từng bước, cập nhật `docs/HUONG_DAN.md` và `docs/KIEN_TRUC.md` cho khớp code hiện tại. Dựng khung báo cáo LaTeX theo cấu trúc: vấn đề → kiến trúc → thực nghiệm → kết quả → giới hạn.

## Bản đồ file — tránh conflict

| Terminal | Được sửa | KHÔNG sửa |
|---|---|---|
| 1 evals | `evals/cases/*.yaml`, test đếm case | `laplace/evals/*` |
| 2 exp | `laplace/evals/*`, `evals/results/` | `evals/cases/`, `laplace/agent/` |
| 3 harden | `laplace/agent/`, `laplace/tools/`, `tests/` | `laplace/evals/`, docs |
| 4 docs | `Dockerfile`, `README`, `docs/` | mọi file `.py` |

## Thứ tự merge về main

1. **Terminal 1** (chỉ thêm file YAML — merge sớm, không conflict)
2. **Terminal 3** (hardening — merge trước khi chạy số liệu cuối, vì prompt thay đổi ảnh hưởng kết quả eval)
3. **Terminal 2** merge runner, rồi **chạy lại thí nghiệm trên main** sau khi 1+3 đã vào → số liệu cuối cùng
4. **Terminal 4** merge lúc nào cũng được

Mỗi terminal xong việc: `git commit` trên branch của mình, quay về terminal chính `git merge <branch>` theo thứ tự trên, chạy `pytest` sau mỗi lần merge. Dọn worktree khi xong: `git worktree remove ../laplace-<tên>`.

## Lưu ý

- File `.env` không được commit — copy tay sang worktree nào cần chạy LLM thật: `cp .env ../laplace-exp/`.
- `laplace.db` là DB local, mỗi worktree tự tạo riêng khi chạy — không đụng nhau.
- Terminal nào rảnh trước có thể nhận thêm việc từ backlog: streaming status về Telegram, replay trace mode (mục 8 PLAN.md).
