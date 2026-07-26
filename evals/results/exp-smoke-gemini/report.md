# Báo cáo thí nghiệm 2×2 — chiến lược × model

- Thời điểm: 20260726-141649
- Số case: 4 · Run/cấu hình: 1 · Model: {'gemini': 'gemini-3.1-flash-lite'}

## Bảng so sánh metric

| Metric | `react__gemini` |
|---|---|
| success_rate | 0.25 |
| route_accuracy | 0.667 |
| tool_selection_accuracy | 0.25 |
| recovery_rate | 0.0 |
| avg_steps | 0.75 |
| avg_llm_calls | 2.75 |
| avg_tokens | 2351.8 |
| avg_cost_usd | 0.000339 |
| avg_duration_s | 3.949 |
| p95_duration_s | 4.514 |
| total_retries | 0 |
| runs_total | 4 |

> Lưu ý: với provider `mock`, chỉ ô có chiến lược trùng chiến lược gốc của case
> mới có ý nghĩa số liệu (mock_script gắn với chiến lược gốc; ô còn lại chạy
> MockLLM heuristic để kiểm tra runner, đa phần không đạt expected).

## Thất bại — `react__gemini` (3)

- `single_task_add` (run 0): tools
- `multi_report_react` (run 0): route, tools
- `recovery_fetch_fails_fallback_search` (run 0): tools, answer_contains
