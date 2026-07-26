"""Test khung eval harness: chay tron bo case offline (MockLLM script)."""

from pathlib import Path

from laplace.evals.harness import EvalCase, aggregate, load_cases, run_case, run_suite
from laplace.llm.mock import MockLLM

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


def _judged_case() -> EvalCase:
    """Case direct don gian co field judge; agent loop van chay bang mock_script rieng."""
    return EvalCase(
        id="judged_compare",
        request="So sánh FastAPI và Flask giúp tôi",
        expected={"status": "done", "route": "direct", "tools": []},
        mock_script=[
            {"route": "direct", "reason": "general knowledge"},
            "FastAPI hỗ trợ async và tự sinh OpenAPI docs; Flask đơn giản, hệ sinh thái lớn.",
        ],
        judge="Câu trả lời phải nêu ít nhất 2 điểm khác nhau giữa FastAPI và Flask",
    )


def test_judge_pass_adds_check(tmp_path):
    judge_llm = MockLLM(script=[{"passed": True, "reason": "ok"}])
    row = run_case(_judged_case(), provider="mock", workdir=tmp_path, judge_llm=judge_llm)
    assert row["checks"]["judge"] is True
    assert row["passed"] is True
    assert row["judge_reason"] == "ok"
    assert len(judge_llm.calls) == 1  # judge chi bi goi 1 lan khi tra dung schema


def test_judge_fail_marks_run_failed(tmp_path):
    judge_llm = MockLLM(script=[{"passed": False, "reason": "thieu y"}])
    row = run_case(_judged_case(), provider="mock", workdir=tmp_path, judge_llm=judge_llm)
    assert row["checks"]["judge"] is False
    assert row["passed"] is False
    assert row["judge_reason"] == "thieu y"

    summary = aggregate([row])
    assert summary["judge_pass_rate"] == 0.0
    assert summary["failures"][0]["id"] == "judged_compare"
    assert "judge" in summary["failures"][0]["failed_checks"]


def test_judge_skipped_without_judge_llm(tmp_path):
    row = run_case(_judged_case(), provider="mock", workdir=tmp_path)
    assert "judge" not in row["checks"]
    assert row["passed"] is True  # rule-based van pass y nhu cu
    assert row["judge_reason"] is None
    assert aggregate([row])["judge_pass_rate"] is None
