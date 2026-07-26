"""Test khung eval harness: chay tron bo case offline (MockLLM script)."""

import json
from pathlib import Path

from laplace.evals import experiment
from laplace.evals.experiment import run_experiment
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


# ---------------------------------------------------------------------------
# Experiment runner 2x2 (laplace/evals/experiment.py)
# ---------------------------------------------------------------------------


def _mini_cases() -> list[EvalCase]:
    return [
        EvalCase(
            id="exp_direct",
            request="chào bạn",
            expected={"status": "done", "route": "direct", "tools": []},
            mock_script=[{"route": "direct", "reason": "greeting"}, "chào lại bạn"],
        ),
    ]


def test_experiment_matrix_writes_outputs_and_resumes(tmp_path):
    cases = _mini_cases()
    res = run_experiment(cases, strategies=["react", "plan_execute"], providers=["mock"],
                         runs=2, out_dir=tmp_path, name="t1", charts=False)
    assert res["meta"]["executed"] == 4  # 2 chien luoc x 1 case x 2 run
    exp_dir = Path(res["out_dir"])
    assert (exp_dir / "checkpoint.jsonl").exists()
    assert (exp_dir / "results.json").exists()
    assert (exp_dir / "report.md").exists()
    assert set(res["summaries"]) == {"react__mock", "plan_execute__mock"}
    # chien luoc goc (react) giu duoc mock_script -> pass tron
    assert res["summaries"]["react__mock"]["success_rate"] == 1.0
    # khong con file db tam trong thu muc ket qua
    assert not list(exp_dir.rglob("*.db"))

    # resume: chay lai cung name -> khong run nao chay them
    res2 = run_experiment(cases, strategies=["react", "plan_execute"], providers=["mock"],
                          runs=2, out_dir=tmp_path, name="t1", charts=False)
    assert res2["meta"]["executed"] == 0
    assert res2["meta"]["resumed_from_checkpoint"] == 4
    assert res2["summaries"]["react__mock"]["runs_total"] == 2


def test_experiment_forces_strategy_and_drops_stale_script():
    cfg = experiment.ExpConfig(strategy="plan_execute", provider="mock")
    case = _mini_cases()[0]
    prepared = experiment._prepare_case(case, cfg)
    assert prepared.strategy == "plan_execute"
    assert prepared.mock_script is None  # script viet cho react -> phai bo
    assert case.strategy == "react"  # case goc khong bi sua

    same = experiment._prepare_case(case, experiment.ExpConfig("react", "mock"))
    assert same.mock_script is not None


def test_experiment_retries_on_rate_limit(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_run_case(case, *, provider, workdir, run_idx=0, judge_llm=None):
        calls["n"] += 1
        err = "RateLimitError: 429, please retry in 7s" if calls["n"] == 1 else None
        return {"id": case.id, "run": run_idx, "strategy": case.strategy, "tags": [],
                "status": "done", "route": "direct", "checks": {"status": err is None},
                "passed": err is None, "judge_reason": None, "error": err,
                "steps": 0, "llm_calls": 1, "tokens": 20, "prompt_tokens": 10,
                "completion_tokens": 10, "cost_usd": 0.0, "duration_s": 0.01}

    slept: list[float] = []
    monkeypatch.setattr(experiment, "run_case", fake_run_case)
    monkeypatch.setattr(experiment.time, "sleep", slept.append)
    res = run_experiment(_mini_cases(), strategies=["react"], providers=["mock"],
                         runs=1, out_dir=tmp_path, name="t2", charts=False)
    assert calls["n"] == 2  # lan 1 dinh 429, lan 2 ok
    assert slept == [8.0]  # doc "retry in 7s" tu thong bao loi + 1s dem
    rows = json.loads((Path(res["out_dir"]) / "results.json").read_text())["runs"]
    assert rows[0]["retries"] == 1
    assert rows[0]["passed"] is True


def test_experiment_estimates_cost_for_unpriced_model():
    row = {"cost_usd": 0.0, "tokens": 1_000_000,
           "prompt_tokens": 500_000, "completion_tokens": 500_000}
    experiment._estimate_cost(row, "gemini")  # gemini-3.1-flash-lite: fallback pricing
    assert row["cost_usd"] > 0
    assert row["cost_estimated"] is True
