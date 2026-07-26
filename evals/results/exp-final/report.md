# Báo cáo thí nghiệm 2×2 — chiến lược × model

- Thời điểm: 20260726-160843
- Số case: 66 · Run/cấu hình: 3 · Model: {'mock': 'mock'}

## Bảng so sánh metric

| Metric | `react__mock` | `plan_execute__mock` |
|---|---|---|
| success_rate | 0.864 | 0.212 |
| route_accuracy | 0.842 | 0.263 |
| tool_selection_accuracy | 0.892 | 0.369 |
| judge_pass_rate | 1.0 | 0.0 |
| recovery_rate | 0.667 | 0.333 |
| avg_self_corrections | 0 | 0 |
| avg_steps | 1.15 | 0.3 |
| avg_llm_calls | 3.14 | 2.41 |
| avg_tokens | 57.3 | 13.6 |
| avg_cost_usd | 0.0 | 0.0 |
| avg_duration_s | 0.023 | 0.012 |
| p95_duration_s | 0.042 | 0.029 |
| total_retries | 0 | 0 |
| runs_total | 198 | 198 |

> Lưu ý: với provider `mock`, chỉ ô có chiến lược trùng chiến lược gốc của case
> mới có ý nghĩa số liệu (mock_script gắn với chiến lược gốc; ô còn lại chạy
> MockLLM heuristic để kiểm tra runner, đa phần không đạt expected).

## Thất bại — `react__mock` (27)

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
- `long_plan_execute_db_research` (run 0): route, tools
- `long_plan_execute_db_research` (run 1): route, tools
- `long_plan_execute_db_research` (run 2): route, tools
- `cfm2_scheduler_rejected_plan` (run 0): status, route
- `cfm2_scheduler_rejected_plan` (run 1): status, route
- `cfm2_scheduler_rejected_plan` (run 2): status, route
- `rec2_fetch_fail_always_replan_search` (run 0): route, tools, answer_contains
- `rec2_fetch_fail_always_replan_search` (run 1): route, tools, answer_contains
- `rec2_fetch_fail_always_replan_search` (run 2): route, tools, answer_contains
- `rec2_plan_execute_fail_once_transparent` (run 0): route, tools
- `rec2_plan_execute_fail_once_transparent` (run 1): route, tools
- `rec2_plan_execute_fail_once_transparent` (run 2): route, tools

## Thất bại — `plan_execute__mock` (156)

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
- `confirm_delete_rejected` (run 0): judge
- `confirm_delete_rejected` (run 1): judge
- `confirm_delete_rejected` (run 2): judge
- `confirm_scheduler_approved` (run 0): route, tools, judge
- `confirm_scheduler_approved` (run 1): route, tools, judge
- `confirm_scheduler_approved` (run 2): route, tools, judge
- `recovery_fetch_fails_fallback_search` (run 0): tools, judge
- `recovery_fetch_fails_fallback_search` (run 1): tools, judge
- `recovery_fetch_fails_fallback_search` (run 2): tools, judge
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
- ... và 116 run khác (xem results.json)
