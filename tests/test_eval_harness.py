"""Test khung eval harness: chay tron bo case offline (MockLLM script)."""

from pathlib import Path

from laplace.evals.harness import EvalCase, aggregate, load_cases, run_case, run_suite

CASES_DIR = Path(__file__).parent.parent / "evals" / "cases"


def test_full_suite_offline_passes(tmp_path):
    cases = load_cases(CASES_DIR)
    assert len(cases) >= 12

    summary = run_suite(cases, provider="mock", runs=1, out_dir=tmp_path)

    assert summary["success_rate"] == 1.0, f"failures: {summary['failures']}"
    assert summary["route_accuracy"] == 1.0
    assert summary["tool_selection_accuracy"] == 1.0
    assert summary["recovery_rate"] == 1.0
    out = Path(summary["out_dir"])
    assert (out / "results.json").exists()
    assert (out / "report.md").exists()


def test_failing_case_is_reported(tmp_path):
    bad = EvalCase(
        id="expect_wrong_tool",
        request="chào bạn",
        expected={"status": "done", "tools": ["web_search"]},  # thuc te khong goi tool nao
        mock_script=[{"route": "direct", "reason": "greeting"}, "chào lại"],
    )
    row = run_case(bad, provider="mock", workdir=tmp_path)
    assert row["passed"] is False
    assert row["checks"]["tools"] is False

    summary = aggregate([row])
    assert summary["success_rate"] == 0.0
    assert summary["failures"][0]["id"] == "expect_wrong_tool"
