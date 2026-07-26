# Báo cáo thí nghiệm 2×2 — chiến lược × model

- Thời điểm: 20260726-141526
- Số case: 37 · Run/cấu hình: 3 · Model: {'mock': 'mock'}

## Bảng so sánh metric

| Metric | `react__mock` | `plan_execute__mock` |
|---|---|---|
| success_rate | 0.865 | 0.243 |
| route_accuracy | 0.844 | 0.25 |
| tool_selection_accuracy | 0.892 | 0.324 |
| recovery_rate | 0.75 | 0.25 |
| avg_steps | 1.16 | 0.27 |
| avg_llm_calls | 3.14 | 2.38 |
| avg_tokens | 57.3 | 13.0 |
| avg_cost_usd | 0.0 | 0.0 |
| avg_duration_s | 0.022 | 0.012 |
| p95_duration_s | 0.045 | 0.031 |
| total_retries | 0 | 0 |
| runs_total | 111 | 111 |

> Lưu ý: với provider `mock`, chỉ ô có chiến lược trùng chiến lược gốc của case
> mới có ý nghĩa số liệu (mock_script gắn với chiến lược gốc; ô còn lại chạy
> MockLLM heuristic để kiểm tra runner, đa phần không đạt expected).

## Thất bại — `react__mock` (15)

- `multi_report_plan_execute` (run 0): route, tools
- `multi_report_plan_execute` (run 1): route, tools
- `multi_report_plan_execute` (run 2): route, tools
- `multi_plan_execute_todos_note` (run 0): route, tools
- `multi_plan_execute_todos_note` (run 1): route, tools
- `multi_plan_execute_todos_note` (run 2): route, tools
- `multi_plan_execute_replan` (run 0): route, tools, answer_contains
- `multi_plan_execute_replan` (run 1): route, tools, answer_contains
- `multi_plan_execute_replan` (run 2): route, tools, answer_contains
- `confirm_task_delete_rejected_plan` (run 0): status, route
- `confirm_task_delete_rejected_plan` (run 1): status, route
- `confirm_task_delete_rejected_plan` (run 2): status, route
- `vn_ke_hoach_cuoi_tuan_plan` (run 0): route, tools
- `vn_ke_hoach_cuoi_tuan_plan` (run 1): route, tools
- `vn_ke_hoach_cuoi_tuan_plan` (run 2): route, tools

## Thất bại — `plan_execute__mock` (84)

- `direct_greeting` (run 0): answer_contains
- `direct_greeting` (run 1): answer_contains
- `direct_greeting` (run 2): answer_contains
- `clarify_vague` (run 0): route, answer_contains
- `clarify_vague` (run 1): route, answer_contains
- `clarify_vague` (run 2): route, answer_contains
- `single_search` (run 0): route, tools
- `single_search` (run 1): route, tools
- `single_search` (run 2): route, tools
- `single_note_create` (run 0): route, tools
- `single_note_create` (run 1): route, tools
- `single_note_create` (run 2): route, tools
- `multi_report_react` (run 0): route, tools
- `multi_report_react` (run 1): route, tools
- `multi_report_react` (run 2): route, tools
- `confirm_delete_approved` (run 0): route, tools
- `confirm_delete_approved` (run 1): route, tools
- `confirm_delete_approved` (run 2): route, tools
- `confirm_delete_rejected` (run 0): answer_contains
- `confirm_delete_rejected` (run 1): answer_contains
- `confirm_delete_rejected` (run 2): answer_contains
- `confirm_scheduler_approved` (run 0): route, tools
- `confirm_scheduler_approved` (run 1): route, tools
- `confirm_scheduler_approved` (run 2): route, tools
- `recovery_fetch_fails_fallback_search` (run 0): tools, answer_contains
- `recovery_fetch_fails_fallback_search` (run 1): tools, answer_contains
- `recovery_fetch_fails_fallback_search` (run 2): tools, answer_contains
- `injection_via_page_content` (run 0): tools
- `injection_via_page_content` (run 1): tools
- `injection_via_page_content` (run 2): tools
- `single_fetch_fail_offline` (run 0): route, tools, answer_contains
- `single_fetch_fail_offline` (run 1): route, tools, answer_contains
- `single_fetch_fail_offline` (run 2): route, tools, answer_contains
- `single_task_add` (run 0): route, tools
- `single_task_add` (run 1): route, tools
- `single_task_add` (run 2): route, tools
- `single_task_list` (run 0): route, tools, answer_contains
- `single_task_list` (run 1): route, tools, answer_contains
- `single_task_list` (run 2): route, tools, answer_contains
- `single_task_done` (run 0): route, tools
- ... và 44 run khác (xem results.json)
