"""Eval harness: chay bo test case co dinh qua agent loop that, cham rule-based.

Moi case la mot muc YAML gom: request, ky vong (status/route/tools/answer),
kich ban confirm, tool can gia lap loi, va (cho che do offline) mock_script —
chuoi phan hoi LLM de MockLLM phat lai. Cung mot bo case:

- provider=mock  -> chay offline, deterministic (kiem tra khung + regression)
- provider=openai -> chay LLM that, mock_script bi bo qua -> so lieu thuc

LLM-as-judge (tuy chon): case co field `judge` (tieu chi bang ngon ngu tu nhien)
se duoc cham them boi mot LLM rieng khi run_suite nhan `judge_provider` (hoac
run_case nhan `judge_llm`). Khong truyen judge thi case chay rule-based y nhu cu.
Luu y: judge dung CHINH provider abstraction (get_provider), nen khi thi nghiem
can tach model judge KHAC model agent de tranh thien vi (self-preference bias).

Ket qua: results.json (tung run) + report.md (bang tong hop) trong out_dir.
"""

import json
import statistics
import time
from contextlib import chdir
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from laplace.agent.orchestrator import resume_task, run_task
from laplace.db import init_db, reset_engine_for_tests, session_scope
from laplace.llm.base import LLMProvider, get_provider
from laplace.llm.mock import MockLLM
from laplace.models import Note, Todo
from laplace.schemas import ToolResult
from laplace.services.tasks import create_task, get_or_create_user
from laplace.services.trace import task_trace
from laplace.tools.base import get_tool, load_builtin_tools

MAX_CONFIRM_ROUNDS = 3


@dataclass
class EvalCase:
    id: str
    request: str
    strategy: str = "react"
    tags: list[str] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    confirm: str | None = None  # approve | reject
    patch_tools: dict[str, str] = field(default_factory=dict)  # {tool: fail_once|fail_always}
    mock_script: list[Any] | None = None
    seed: dict[str, list[str]] = field(default_factory=dict)  # {notes: [...], todos: [...]}
    judge: str | None = None  # tieu chi cham LLM-as-judge (ngon ngu tu nhien), tuy chon


class JudgeVerdict(BaseModel):
    """Ket luan cua LLM judge cho mot run."""

    passed: bool
    reason: str


def load_cases(path: str | Path) -> list[EvalCase]:
    """Nap case tu 1 file YAML hoac tat ca *.yaml trong thu muc (sap xep theo ten)."""
    path = Path(path)
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    cases: list[EvalCase] = []
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for raw in data.get("cases", []):
            cases.append(EvalCase(**raw))
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Trung id case: {dupes}")
    return cases


def _seed_data(user_id: int, seed: dict[str, list[str]]) -> None:
    with session_scope() as session:
        for text in seed.get("todos", []):
            session.add(Todo(user_id=user_id, text=text))
        for title in seed.get("notes", []):
            session.add(Note(user_id=user_id, title=title))


def _patch_tools(patch: dict[str, str]) -> list[tuple[str, Any]]:
    """Boc tool de gia lap loi; tra ve danh sach (name, fn goc) de restore."""
    originals: list[tuple[str, Any]] = []
    for name, mode in patch.items():
        spec = get_tool(name)
        if spec is None:
            raise ValueError(f"patch_tools: tool '{name}' khong ton tai")
        orig = spec.fn
        counter = {"n": 0}

        def wrapper(params, ctx, _orig=orig, _mode=mode, _counter=counter):
            _counter["n"] += 1
            if _mode == "fail_always" or (_mode == "fail_once" and _counter["n"] == 1):
                return ToolResult(ok=False, error="simulated failure (eval harness)")
            return _orig(params, ctx)

        spec.fn = wrapper
        originals.append((name, orig))
    return originals


def _restore_tools(originals: list[tuple[str, Any]]) -> None:
    for name, orig in originals:
        spec = get_tool(name)
        if spec is not None:
            spec.fn = orig


def _score(case: EvalCase, trace: dict[str, Any]) -> dict[str, bool]:
    """Cham rule-based theo case.expected. Moi key la mot check pass/fail."""
    exp = case.expected or {}
    task = trace["task"]
    checks: dict[str, bool] = {"status": task["status"] == exp.get("status", "done")}
    if "route" in exp:
        checks["route"] = task.get("route") == exp["route"]
    executed = [s["tool"] for s in trace["steps"] if s["status"] in ("ok", "error")]
    if "tools" in exp:
        mode = exp.get("tools_match", "set")
        if mode == "exact":
            checks["tools"] = executed == list(exp["tools"])
        elif mode == "subset":
            checks["tools"] = set(exp["tools"]) <= set(executed)
        else:  # set
            checks["tools"] = set(executed) == set(exp["tools"])
    if "forbid_tools" in exp:
        checks["forbid_tools"] = not (set(exp["forbid_tools"]) & set(executed))
    if "answer_contains" in exp:
        answer = (task.get("result") or "").lower()
        checks["answer_contains"] = all(s.lower() in answer for s in exp["answer_contains"])
    return checks


_JUDGE_SYSTEM = (
    "You are a strict evaluator for an AI assistant. Decide whether the final "
    "answer satisfies ALL of the given criteria. Respond with JSON matching the "
    'schema {"passed": boolean, "reason": string}. Keep the reason short and concrete.'
)


def _judge(llm: LLMProvider, case: EvalCase, final_answer: str) -> JudgeVerdict:
    """Cham chat luong cau tra loi bang LLM theo case.judge.

    Sai schema thi retry 1 lan; van sai -> coi nhu judge fail voi reason ro rang.
    """
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## User request\n{case.request}\n\n"
                f"## Final answer\n{final_answer}\n\n"
                f"## Criteria\n{case.judge}"
            ),
        },
    ]
    schema = JudgeVerdict.model_json_schema()
    for _attempt in range(2):
        result = llm.complete(messages, json_schema=schema)
        payload: Any = result.parsed
        if payload is None and result.content:
            try:
                payload = json.loads(result.content)
            except ValueError:
                payload = None
        try:
            return JudgeVerdict.model_validate(payload)
        except ValidationError:
            continue
    return JudgeVerdict(
        passed=False,
        reason='judge LLM did not return valid {"passed", "reason"} JSON after 1 retry',
    )


def run_case(
    case: EvalCase,
    *,
    provider: str = "mock",
    workdir: Path,
    run_idx: int = 0,
    judge_llm: LLMProvider | None = None,
) -> dict[str, Any]:
    """Chay 1 case tren mot DB SQLite moi tinh, tra ve ket qua + metric."""
    db_file = workdir / f"db_{case.id}_{run_idx}.db"
    reset_engine_for_tests(f"sqlite:///{db_file}")
    init_db()
    load_builtin_tools()

    if provider == "mock":
        script = None
        if case.mock_script is not None:
            script = [dict(x) if isinstance(x, dict) else x for x in case.mock_script]
        llm = MockLLM(script=script)
    else:
        llm = get_provider(provider)

    with session_scope() as session:
        user = get_or_create_user(session)
        user_id = user.id
    _seed_data(user_id, case.seed)
    with session_scope() as session:
        task = create_task(session, user_id=user_id, request=case.request,
                           strategy=case.strategy)
        task_id = task.id

    originals = _patch_tools(case.patch_tools)
    start = time.monotonic()
    error: str | None = None
    try:
        task = run_task(task_id, llm=llm)
        rounds = 0
        while task.status == "awaiting_confirm" and case.confirm and rounds < MAX_CONFIRM_ROUNDS:
            task = resume_task(task_id, approved=case.confirm == "approve", llm=llm)
            rounds += 1
    except Exception as e:  # loi ha tang cung phai vao bao cao thay vi sap ca suite
        error = f"{type(e).__name__}: {e}"
    finally:
        _restore_tools(originals)
    duration_s = round(time.monotonic() - start, 3)

    with session_scope() as session:
        trace = task_trace(session, task_id)
    checks = _score(case, trace) if not error else {"no_crash": False}
    judge_reason: str | None = None
    if case.judge and judge_llm is not None and not error:
        verdict = _judge(judge_llm, case, trace["task"].get("result") or "")
        checks["judge"] = verdict.passed
        judge_reason = verdict.reason
    return {
        "id": case.id,
        "run": run_idx,
        "strategy": case.strategy,
        "tags": case.tags,
        "status": trace["task"]["status"] if trace else "?",
        "route": trace["task"].get("route") if trace else None,
        "checks": checks,
        "passed": all(checks.values()),
        "judge_reason": judge_reason,
        "error": error,
        "steps": trace["totals"]["steps"] if trace else 0,
        "llm_calls": len(trace["llm_calls"]) if trace else 0,
        # So lan self-correction (T8): retry LLM mang purpose "<purpose>:fixN"
        "self_corrections": sum(1 for c in trace["llm_calls"] if ":fix" in c["purpose"])
        if trace else 0,
        "tokens": (trace["totals"]["prompt_tokens"] + trace["totals"]["completion_tokens"])
        if trace else 0,
        "prompt_tokens": trace["totals"]["prompt_tokens"] if trace else 0,
        "completion_tokens": trace["totals"]["completion_tokens"] if trace else 0,
        "cost_usd": trace["totals"]["cost_usd"] if trace else 0.0,
        "duration_s": duration_s,
    }


def _rate(rows: list[dict], key: str) -> float | None:
    scored = [r for r in rows if key in r["checks"]]
    if not scored:
        return None
    return round(sum(r["checks"][key] for r in scored) / len(scored), 3)


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    recovery = [r for r in rows if "recovery" in r["tags"]]
    return {
        "runs_total": len(rows),
        "success_rate": round(sum(r["passed"] for r in rows) / len(rows), 3) if rows else None,
        "route_accuracy": _rate(rows, "route"),
        "tool_selection_accuracy": _rate(rows, "tools"),
        "judge_pass_rate": _rate(rows, "judge"),
        "recovery_rate": round(sum(r["passed"] for r in recovery) / len(recovery), 3)
        if recovery else None,
        "avg_steps": round(statistics.mean(r["steps"] for r in rows), 2) if rows else None,
        "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in rows), 2)
        if rows else None,
        "avg_self_corrections": round(
            statistics.mean(r.get("self_corrections", 0) for r in rows), 3)
        if rows else None,
        "avg_tokens": round(statistics.mean(r["tokens"] for r in rows), 1) if rows else None,
        "avg_cost_usd": round(statistics.mean(r["cost_usd"] for r in rows), 6)
        if rows else None,
        "avg_duration_s": round(statistics.mean(r["duration_s"] for r in rows), 3)
        if rows else None,
        "failures": [
            {"id": r["id"], "run": r["run"], "error": r["error"],
             "failed_checks": [k for k, v in r["checks"].items() if not v]}
            for r in rows if not r["passed"]
        ],
    }


def _write_report(out: Path, meta: dict, rows: list[dict], summary: dict) -> None:
    (out / "results.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "runs": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Eval report",
        "",
        f"- Thời điểm: {meta['timestamp']}",
        (
            f"- Provider: `{meta['provider']}` · Số run/case: {meta['runs']}"
            f" · Tổng case: {meta['n_cases']}"
        ),
        "",
        "## Tổng hợp",
        "",
        "| Metric | Giá trị |",
        "|---|---|",
    ]
    for key in ("success_rate", "route_accuracy", "tool_selection_accuracy",
                "judge_pass_rate", "recovery_rate", "avg_steps", "avg_llm_calls",
                "avg_self_corrections", "avg_tokens", "avg_cost_usd", "avg_duration_s"):
        lines.append(f"| {key} | {summary[key]} |")
    header = (
        "| Case | Run | Strategy | Status | Pass | Judge | Checks lỗi | Steps |"
        " LLM calls | Cost | Giây |"
    )
    lines += ["", "## Từng run", "", header, "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        failed = ", ".join(k for k, v in r["checks"].items() if not v) or "—"
        judge_cell = "—" if "judge" not in r["checks"] else (
            "✅" if r["checks"]["judge"] else "❌"
        )
        lines.append(
            f"| {r['id']} | {r['run']} | {r['strategy']} | {r['status']} |"
            f" {'✅' if r['passed'] else '❌'} | {judge_cell} | {failed} | {r['steps']} |"
            f" {r['llm_calls']} | {r['cost_usd']} | {r['duration_s']} |"
        )
    judge_fails = [r for r in rows if r["checks"].get("judge") is False]
    if judge_fails:
        lines += ["", "## Judge fail", ""]
        for r in judge_fails:
            lines.append(f"- `{r['id']}` (run {r['run']}): {r.get('judge_reason')}")
    if summary["failures"]:
        lines += ["", "## Thất bại", ""]
        for f in summary["failures"]:
            detail = f["error"] or ", ".join(f["failed_checks"])
            lines.append(f"- `{f['id']}` (run {f['run']}): {detail}")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_suite(
    cases: list[EvalCase],
    *,
    provider: str = "mock",
    runs: int = 1,
    out_dir: str | Path = "eval_results",
    judge_provider: str | None = None,
) -> dict[str, Any]:
    """Chay toan bo case (moi case `runs` lan), ghi report, tra ve summary.

    judge_provider: neu truyen, tao judge_llm MOT lan qua get_provider() va cham
    LLM-as-judge cho cac case co field `judge`. Nen dung model khac model agent.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = Path(out_dir) / f"{stamp}-{provider}"
    out.mkdir(parents=True, exist_ok=True)
    judge_llm = get_provider(judge_provider) if judge_provider else None
    rows: list[dict[str, Any]] = []
    with chdir(out):  # file phu (reports/ cua report_builder, db) nam gon trong out dir
        for case in cases:
            for i in range(runs):
                rows.append(run_case(case, provider=provider, workdir=Path("."),
                                     run_idx=i, judge_llm=judge_llm))
    summary = aggregate(rows)
    meta = {"timestamp": stamp, "provider": provider, "runs": runs, "n_cases": len(cases),
            "judge_provider": judge_provider}
    _write_report(out, meta, rows, summary)
    summary["out_dir"] = str(out)
    return summary
