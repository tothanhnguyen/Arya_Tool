# Plan làm việc song song — nhiều terminal Claude Code

> Mục tiêu: hoàn thành giai đoạn 4 (đánh giá & tối ưu) của `PLAN.md` nhanh hơn bằng 4 terminal chạy song song, mỗi terminal một git worktree riêng để không giẫm chân nhau.

## Bước 0 — Chuẩn bị (terminal chính, làm TRƯỚC, ~5 phút)

Main đã sạch (commit `45238c0`, 48 test pass, 37 case mock 37/37) — chỉ cần tạo worktree:

```bash
cd ~/Documents/Laplace_Demon

# Tạo 4 worktree (thư mục nằm cạnh repo chính)
git worktree add ../laplace-evals   -b feat/eval-cases-60
git worktree add ../laplace-exp     -b feat/experiment-2x2
git worktree add ../laplace-harden  -b feat/hardening
git worktree add ../laplace-docs    -b chore/deploy-docs

# .venv KHÔNG đi theo worktree — mỗi worktree cần venv riêng để chạy pytest/eval:
for d in ../laplace-evals ../laplace-exp ../laplace-harden; do
  (cd $d && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]")
done
# laplace-docs không chạy python, bỏ qua cũng được
```

Sau đó mở 4 cửa terminal, mỗi cửa `cd` vào một thư mục trên và chạy `claude`.

## Phân công 4 terminal

### Terminal 1 — `../laplace-evals` — Mở rộng bộ eval 37 → ~60 case ⭐ dùng nhiều agent — ✅ XONG (66 case, đã merge main `35323a2`, worktree đã dọn)

Hiện đã có 37 case trong 12 file (file 07–12 vừa thêm ở commit `45238c0`). Mỗi file YAML chứa nhiều case; các agent viết file mới song song không đụng nhau.

**Quy ước:** terminal này CHỈ thêm file YAML mới, KHÔNG sửa `laplace/evals/harness.py` và KHÔNG sửa `tests/` (test hiện có dùng `assert len(cases) >= 12` nên thêm case không làm vỡ test — kiểm chứng bằng cách chạy mock eval là đủ).

Prompt gợi ý dán vào Claude:

> Đọc `laplace/evals/harness.py` và các case mẫu trong `evals/cases/` để hiểu format. Hiện có 37 case trong file 01–12. Spawn 6 agent song song, mỗi agent viết 1 file YAML mới chứa 3–5 case cho một nhóm còn mỏng: (1) direct/clarify, (2) scheduler/cron, (3) multi-step dài (4+ bước), (4) confirm-flow từ chối/đổi ý, (5) prompt injection biến thể mới, (6) tool error/recovery. Đánh số file từ 13 trở đi, không trùng tên. Xong thì chạy eval với provider mock để chắc mọi case parse được và pass, rồi commit. KHÔNG sửa file nào ngoài `evals/cases/`.

### Terminal 2 — `../laplace-exp` — Thí nghiệm 2×2 + báo cáo metric

Chạy ma trận {ReAct, Plan-Execute} × {2 model}, ≥3 run/cấu hình, xuất bảng metric + biểu đồ vào `evals/results/`. Job này chạy lâu (rate limit Gemini) → để riêng một terminal là đúng bài.

**Sở hữu file:** `laplace/evals/harness.py`, `laplace/evals/__main__.py`, `evals/results/`, `tests/test_eval_harness.py` (test này import harness — ai sửa harness thì sửa test), `laplace/config.py` (nếu cần thêm cấu hình model thứ 2).

Prompt gợi ý:

> Viết runner thí nghiệm 2×2: chiến lược {react, plan_execute} × model (Gemini + mock hoặc model thứ 2 nếu có key), mỗi cấu hình ≥3 run trên toàn bộ case trong `evals/cases/`. Lưu kết quả thô JSON + bảng tổng hợp 8 metric (success rate, tool accuracy, số bước, latency, cost, recovery...) vào `evals/results/`, sinh biểu đồ so sánh. Chịu được rate limit (retry/backoff, resume được giữa chừng). Chạy thử trước với mock, commit, rồi chạy thật với Gemini.

Phụ thuộc: chạy được ngay với 13 case hiện có; sau khi merge Terminal 1 thì chạy lại trên 40 case để lấy số liệu cuối.

### Terminal 3 — `../laplace-harden` — Hardening + tối ưu prompt

**Sở hữu file:** `laplace/agent/prompts.py`, `laplace/tools/*`, `laplace/agent/orchestrator.py`, `tests/*` TRỪ `tests/test_eval_harness.py` (thuộc Terminal 2).

Prompt gợi ý:

> Hardening theo T16 của PLAN.md: (1) rà soát chống prompt injection trong nội dung fetch từ web (delimiter + system prompt), viết test; (2) error taxonomy + retry/backoff cho tool executor, test lỗi mạng/timeout; (3) rate limit theo user; (4) tối ưu tool description + prompt trong `laplace/agent/prompts.py` cho rõ ràng hơn. Có thể spawn agent song song cho từng mục vì khác file. Chạy đủ `pytest` trước khi commit.

### Terminal 4 — `../laplace-docs` — Deploy + tài liệu

**Sở hữu file:** `Dockerfile`, `docker-compose.yml`, `README.md`, `docs/*`.

Prompt gợi ý:

> Theo T17 của PLAN.md: kiểm tra và hoàn thiện Dockerfile + docker-compose (build được từ máy sạch, healthcheck, volume cho SQLite), viết README cài đặt từng bước, cập nhật `docs/HUONG_DAN.md` và `docs/KIEN_TRUC.md` cho khớp code hiện tại. Dựng khung báo cáo LaTeX theo cấu trúc: vấn đề → kiến trúc → thực nghiệm → kết quả → giới hạn.

## Bản đồ file — tránh conflict

| Terminal | Được sửa | KHÔNG sửa |
|---|---|---|
| 1 evals | `evals/cases/*.yaml` (file mới, số 13+) | mọi thứ khác, kể cả `tests/` |
| 2 exp | `laplace/evals/*`, `evals/results/`, `tests/test_eval_harness.py`, `laplace/config.py` | `evals/cases/`, `laplace/agent/` |
| 3 harden | `laplace/agent/`, `laplace/tools/`, `tests/*` (trừ test_eval_harness) | `laplace/evals/`, `laplace/config.py`, docs |
| 4 docs | `Dockerfile`, `docker-compose.yml`, `README.md`, `docs/` | mọi file `.py` |

## Thứ tự merge về main

1. **Terminal 1** (chỉ thêm file YAML — merge sớm, không conflict)
2. **Terminal 3** (hardening — merge trước khi chạy số liệu cuối, vì prompt thay đổi ảnh hưởng kết quả eval)
3. **Terminal 2** merge runner, rồi **chạy lại thí nghiệm trên main** sau khi 1+3 đã vào → số liệu cuối cùng
4. **Terminal 4** merge lúc nào cũng được

Mỗi terminal xong việc: `git commit` trên branch của mình, quay về terminal chính `git merge <branch>` theo thứ tự trên, chạy `pytest` sau mỗi lần merge. Dọn worktree khi xong: `git worktree remove ../laplace-<tên>`.

## Lưu ý

- File `.env` không được commit — copy tay sang worktree nào cần chạy LLM thật: `cp .env ../laplace-exp/`.
- `laplace.db` là DB local, mỗi worktree tự tạo riêng khi chạy — không đụng nhau.
- Terminal nào rảnh trước: mở **bảng task chung** `/Users/thanhnguyen/Documents/Laplace_Demon/TASKS.md` (đường dẫn tuyệt đối, file nằm ngoài git — chỉ có một bản duy nhất ở repo chính) và nhận task `TODO` theo luật ghi trong đó.
