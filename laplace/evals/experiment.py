"""Runner thi nghiem 2x2: chien luoc {react, plan_execute} x provider {mock, gemini...}.

Chay toan bo case trong evals/cases/ qua tung cau hinh (strategy, provider),
moi cau hinh `runs` lan, roi xuat:

- checkpoint.jsonl : ket qua tho tung run, ghi ngay sau moi run -> resume duoc
- results.json     : meta + summary per-config + toan bo row
- report.md        : bang so sanh 8+ metric giua cac cau hinh
- metrics_grid.png : bieu do so sanh (matplotlib, tuy chon)

Chiu duoc rate limit Gemini o HAI tang:
- tang provider (openai_provider) da retry 429 toi 5 lan voi backoff;
- tang runner: neu row van loi dang rate-limit/mang, retry them ca case voi
  backoff mu (doc "retry in Xs" tu thong bao loi neu co).

Resume: chay lai voi cung --name (hoac duong dan out da ton tai) se doc
checkpoint.jsonl va bo qua cac (config, case, run) da xong. Ctrl+C an toan:
ket qua da chay van duoc tong hop + luu, lan sau chay tiep.

Luu y ve mock trong ma tran: mock_script cua case gan voi chien luoc GOC cua
case. Khi ep chien luoc khac, runner bo script -> MockLLM heuristic (deterministic
nhung da phan khong dat expected). Vi vay o mock chi o (strategy == native) co
y nghia so lieu; cac o con lai dung de kiem tra runner khong sap.

Vi du:
    python -m laplace.evals.experiment                        # full matrix, mock
    python -m laplace.evals.experiment --providers mock,gemini --runs 3
    python -m laplace.evals.experiment --name exp-smoke --providers gemini \
        --strategies react --runs 1 --case-ids greet_direct,todo_add_simple
"""

import argparse
import dataclasses
import json
import re
import statistics
import time
from contextlib import chdir
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from laplace.evals.harness import EvalCase, aggregate, load_cases, run_case

# Loi dang tam thoi (rate limit / mang / server) -> dang retry o tang runner
RETRYABLE_ERROR = re.compile(
    r"RateLimit|429|quota|rate.?limit|ResourceExhausted|APIConnection|"
    r"Timeout|503|500|InternalServer|ServiceUnavailable|overloaded",
    re.IGNORECASE,
)

# Gia fallback (USD per 1M token) cho model chua co trong PRICING cua provider.
# Cost tinh tu day duoc danh dau cost_estimated=True trong row.
EXTRA_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash": (0.30, 2.50),
}

METRIC_KEYS = (
    "success_rate",
    "route_accuracy",
    "tool_selection_accuracy",
    "judge_pass_rate",
    "recovery_rate",
    "avg_self_corrections",
    "avg_steps",
    "avg_llm_calls",
    "avg_tokens",
    "avg_cost_usd",
    "avg_duration_s",
    "p95_duration_s",
)


class QuotaExhausted(Exception):
    """Loi tam thoi (rate limit/quota) van con sau khi da retry het — dung de resume sau."""


@dataclass(frozen=True)
class ExpConfig:
    strategy: str
    provider: str

    @property
    def id(self) -> str:
        return f"{self.strategy}__{self.provider}"


def _provider_model(provider: str) -> str | None:
    from laplace.config import get_settings

    settings = get_settings()
    return {"gemini": settings.gemini_model, "openai": settings.openai_model,
            "mock": "mock"}.get(provider)


def _prepare_case(case: EvalCase, cfg: ExpConfig) -> EvalCase:
    """Ep chien luoc cua config len case; voi mock khac chien luoc goc thi bo script."""
    if case.strategy == cfg.strategy:
        return case
    new = dataclasses.replace(case, strategy=cfg.strategy)
    if cfg.provider == "mock":
        # script duoc viet cho chien luoc goc -> khong dung duoc, chay heuristic
        new = dataclasses.replace(new, mock_script=None)
    return new


def _estimate_cost(row: dict[str, Any], provider: str) -> None:
    """Provider chua co gia trong bang PRICING -> cost 0. Uoc luong lai tu token."""
    if row.get("cost_usd") or not row.get("tokens"):
        return
    model = _provider_model(provider)
    from laplace.llm.openai_provider import PRICING

    price = PRICING.get(model or "") or EXTRA_PRICING.get(model or "")
    if not price:
        return
    inp, out = price
    row["cost_usd"] = round(
        (row.get("prompt_tokens", 0) * inp + row.get("completion_tokens", 0) * out)
        / 1_000_000, 6)
    row["cost_estimated"] = True


def _backoff_delay(error: str, attempt: int, base: float) -> float:
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", error)
    if match:
        return min(float(match.group(1)) + 1.0, 120.0)
    return min(base * (2 ** attempt), 120.0)


def _run_case_resilient(
    case: EvalCase,
    cfg: ExpConfig,
    run_idx: int,
    work: Path,
    *,
    max_retries: int,
    backoff_base: float,
    judge_llm: Any = None,
) -> dict[str, Any]:
    """Chay 1 case; loi dang rate-limit/mang thi retry ca case voi backoff mu."""
    row: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        with chdir(work):
            row = run_case(case, provider=cfg.provider, workdir=Path("."),
                           run_idx=run_idx, judge_llm=judge_llm)
        # Xoa ca db lan -wal/-shm: file WAL mo coi (unlink moi .db truoc day, hoac
        # process bi kill giua chung) lam SQLite bao "disk I/O error" khi mo lai
        # db cung ten o lan resume sau
        for db_file in work.glob(f"db_{case.id}_{run_idx}.db*"):
            db_file.unlink(missing_ok=True)
        error = row.get("error")
        if not error or not RETRYABLE_ERROR.search(error):
            break
        if attempt < max_retries:
            delay = _backoff_delay(error, attempt, backoff_base)
            print(f"    loi tam thoi ({error[:80]}...) -> cho {delay:.0f}s roi thu lai "
                  f"({attempt + 1}/{max_retries})")
            time.sleep(delay)
    row["retries"] = attempt
    row["config"] = cfg.id
    row["provider"] = cfg.provider
    _estimate_cost(row, cfg.provider)
    return row


def _load_checkpoint(path: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    done: dict[tuple[str, str, int], dict[str, Any]] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done[(row["config"], row["id"], row["run"])] = row
    return done


def _append_checkpoint(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _summaries(rows: list[dict[str, Any]],
               configs: list[ExpConfig]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cfg in configs:
        cfg_rows = [r for r in rows if r["config"] == cfg.id]
        if not cfg_rows:
            continue
        summary = aggregate(cfg_rows)
        summary["p95_duration_s"] = (
            round(statistics.quantiles([r["duration_s"] for r in cfg_rows], n=20)[18], 3)
            if len(cfg_rows) >= 2 else cfg_rows[0]["duration_s"])
        summary["total_retries"] = sum(r.get("retries", 0) for r in cfg_rows)
        out[cfg.id] = summary
    return out


def _hypothesis_section(summaries: dict[str, dict[str, Any]],
                        meta: dict[str, Any]) -> list[str]:
    """Doi chieu gia thuyet: Plan-Execute it buoc/re hon vs ReAct phuc hoi tot hon.

    Chi xet provider LLM that (khac mock); can du ca 2 chien luoc de so sanh.
    """
    llm_providers = [p for p in meta.get("providers", []) if p != "mock"]
    lines: list[str] = []
    for provider in llm_providers:
        react = summaries.get(f"react__{provider}")
        plan = summaries.get(f"plan_execute__{provider}")
        if not react or not plan:
            continue
        lines += ["", f"## Đối chiếu giả thuyết — `{provider}`", ""]

        def cmp(metric: str, label: str, lower_better: bool) -> str:
            rv, pv = react.get(metric), plan.get(metric)
            if rv is None or pv is None:
                return f"- **{label}**: thiếu số liệu ({metric}: react={rv}, plan={pv})."
            plan_wins = (pv < rv) if lower_better else (pv > rv)
            winner = "plan_execute" if plan_wins else ("react" if pv != rv else "hòa")
            return (f"- **{label}** ({metric}): react={rv} vs plan_execute={pv}"
                    f" → nghiêng về `{winner}`.")

        lines.append(cmp("avg_steps", "Ít bước hơn (kỳ vọng: plan_execute)", True))
        lines.append(cmp("avg_llm_calls", "Ít lượt gọi LLM hơn (kỳ vọng: plan_execute)", True))
        lines.append(cmp("avg_cost_usd", "Rẻ hơn (kỳ vọng: plan_execute)", True))
        lines.append(cmp("avg_tokens", "Ít token hơn (kỳ vọng: plan_execute)", True))
        lines.append(cmp("recovery_rate", "Phục hồi lỗi tốt hơn (kỳ vọng: react)", False))
        lines.append(cmp("success_rate", "Success rate tổng thể", False))
        lines.append(cmp("avg_self_corrections", "Ít self-correction hơn", True))

        steps_ok = (plan.get("avg_steps") or 0) < (react.get("avg_steps") or 0)
        cost_ok = (plan.get("avg_cost_usd") or 0) < (react.get("avg_cost_usd") or 0)
        rec_r, rec_p = react.get("recovery_rate"), plan.get("recovery_rate")
        rec_ok = rec_r is not None and rec_p is not None and rec_r > rec_p
        verdict1 = "ĐƯỢC ủng hộ" if (steps_ok and cost_ok) else (
            "ủng hộ MỘT PHẦN" if (steps_ok or cost_ok) else "KHÔNG được ủng hộ")
        verdict2 = ("ĐƯỢC ủng hộ" if rec_ok else "KHÔNG được ủng hộ") if (
            rec_r is not None and rec_p is not None) else "thiếu số liệu"
        lines += [
            "",
            f"**Kết luận**: Giả thuyết \"Plan-Execute ít bước/rẻ hơn\" {verdict1} "
            f"(avg_steps {react.get('avg_steps')}→{plan.get('avg_steps')}, "
            f"avg_cost_usd {react.get('avg_cost_usd')}→{plan.get('avg_cost_usd')}); "
            f"giả thuyết \"ReAct phục hồi lỗi tốt hơn\" {verdict2} "
            f"(recovery_rate react={rec_r} vs plan_execute={rec_p}).",
        ]
    return lines


def _write_report(exp_dir: Path, meta: dict[str, Any],
                  summaries: dict[str, dict[str, Any]],
                  rows: list[dict[str, Any]]) -> None:
    (exp_dir / "results.json").write_text(
        json.dumps({"meta": meta, "summaries": summaries, "runs": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_ids = list(summaries)
    lines = [
        "# Báo cáo thí nghiệm 2×2 — chiến lược × model",
        "",
        f"- Thời điểm: {meta['timestamp']}" + (" (partial — bị ngắt giữa chừng)"
                                               if meta.get("partial") else ""),
        f"- Số case: {meta['n_cases']} · Run/cấu hình: {meta['runs']}"
        f" · Model: {meta['models']}",
        "",
        "## Bảng so sánh metric",
        "",
        "| Metric | " + " | ".join(f"`{c}`" for c in cfg_ids) + " |",
        "|---|" + "---|" * len(cfg_ids),
    ]
    for key in (*METRIC_KEYS, "total_retries", "runs_total"):
        cells = [str(summaries[c].get(key)) for c in cfg_ids]
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "> Lưu ý: với provider `mock`, chỉ ô có chiến lược trùng chiến lược gốc của case",
        "> mới có ý nghĩa số liệu (mock_script gắn với chiến lược gốc; ô còn lại chạy",
        "> MockLLM heuristic để kiểm tra runner, đa phần không đạt expected).",
    ]
    lines += _hypothesis_section(summaries, meta)
    for cfg_id in cfg_ids:
        failures = summaries[cfg_id]["failures"]
        if not failures:
            continue
        lines += ["", f"## Thất bại — `{cfg_id}` ({len(failures)})", ""]
        for f in failures[:40]:
            detail = f["error"] or ", ".join(f["failed_checks"])
            lines.append(f"- `{f['id']}` (run {f['run']}): {detail}")
        if len(failures) > 40:
            lines.append(f"- ... và {len(failures) - 40} run khác (xem results.json)")
    (exp_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Bieu do so sanh (matplotlib — tuy chon, thieu lib thi bo qua)
# ---------------------------------------------------------------------------

# Palette da validate (dataviz skill, light mode): slot 1 blue, slot 2 orange
_SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
_SURFACE, _INK, _INK_2 = "#fcfcfb", "#0b0b0b", "#52514e"
_MUTED, _GRID, _BASELINE = "#898781", "#e1e0d9", "#c3c2b7"


def make_charts(exp_dir: Path, summaries: dict[str, dict[str, Any]],
                strategies: list[str], providers: list[str]) -> Path | None:
    try:
        import matplotlib
    except ImportError:
        print("matplotlib chua cai — bo qua bieu do (pip install matplotlib)")
        return None
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 4, figsize=(16, 9.5), facecolor=_SURFACE)
    fig.suptitle("Thí nghiệm 2×2 — chiến lược × model", fontsize=13,
                 fontweight="bold", color=_INK, x=0.02, ha="left")
    x = range(len(strategies))
    width = 0.8 / max(len(providers), 1)
    for ax, key in zip(axes.flat, METRIC_KEYS, strict=False):
        ax.set_facecolor(_SURFACE)
        for i, provider in enumerate(providers):
            vals = []
            for s in strategies:
                summary = summaries.get(f"{s}__{provider}", {})
                v = summary.get(key)
                vals.append(v if isinstance(v, (int, float)) else 0.0)
            offs = [xi + (i - (len(providers) - 1) / 2) * width for xi in x]
            bars = ax.bar(offs, vals, width * 0.92, label=provider,
                          color=_SERIES_COLORS[i % len(_SERIES_COLORS)],
                          edgecolor=_SURFACE, linewidth=1.5, zorder=3)
            for b, v in zip(bars, vals, strict=True):
                if v:
                    ax.annotate(f"{v:g}", (b.get_x() + b.get_width() / 2, v),
                                ha="center", va="bottom", fontsize=7.5, color=_INK_2)
        ax.set_title(key, fontsize=9.5, color=_INK_2, loc="left")
        ax.set_xticks(list(x))
        ax.set_xticklabels(strategies, fontsize=8.5, color=_MUTED)
        ax.tick_params(axis="y", labelsize=8, colors=_MUTED, length=0)
        ax.grid(axis="y", color=_GRID, linewidth=0.8, zorder=0)
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(_BASELINE)
        if key.endswith("_rate") or key.endswith("accuracy"):
            ax.set_ylim(0, 1.12)
        else:
            ax.set_ylim(bottom=0)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(providers), frameon=False,
               fontsize=9, labelcolor=_INK_2, bbox_to_anchor=(0.99, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = exp_dir / "metrics_grid.png"
    fig.savefig(out, dpi=150, facecolor=_SURFACE)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Vong lap chinh
# ---------------------------------------------------------------------------

def run_experiment(
    cases: list[EvalCase],
    *,
    strategies: list[str],
    providers: list[str],
    runs: int = 3,
    out_dir: str | Path = "evals/results",
    name: str | None = None,
    max_case_retries: int = 3,
    backoff_base: float = 20.0,
    charts: bool = True,
    judge_provider: str | None = None,
) -> dict[str, Any]:
    """Chay ma tran strategy x provider, checkpoint tung run, tong hop + bieu do.

    judge_provider: bat LLM-as-judge cho case co field `judge` (xem harness).
    Tra ve dict {"meta", "summaries"}; meta["executed"] = so run thuc chay lan nay
    (0 nghia la moi thu da co trong checkpoint — resume xong tu truoc).
    """
    name = name or f"exp-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    exp_dir = Path(out_dir) / name
    exp_dir.mkdir(parents=True, exist_ok=True)
    work = exp_dir / "work"
    work.mkdir(exist_ok=True)
    # Don sach db/-wal/-shm sot lai tu lan chay truoc (kill giua chung) truoc khi resume
    for stale in work.glob("*.db*"):
        stale.unlink(missing_ok=True)
    ckpt = exp_dir / "checkpoint.jsonl"
    judge_llm = None
    if judge_provider:
        from laplace.llm.base import get_provider

        judge_llm = get_provider(judge_provider)

    configs = [ExpConfig(strategy=s, provider=p) for p in providers for s in strategies]
    done = _load_checkpoint(ckpt)
    total = len(configs) * len(cases) * runs
    print(f"Thí nghiệm '{name}': {len(configs)} cấu hình × {len(cases)} case × {runs} run"
          f" = {total} run; đã có checkpoint: {len(done)}")

    executed = 0
    partial = False
    try:
        for cfg in configs:
            for case in cases:
                prepared = _prepare_case(case, cfg)
                for run_idx in range(runs):
                    key = (cfg.id, case.id, run_idx)
                    if key in done:
                        continue
                    row = _run_case_resilient(
                        prepared, cfg, run_idx, work,
                        max_retries=max_case_retries, backoff_base=backoff_base,
                        judge_llm=judge_llm)
                    if row.get("error") and RETRYABLE_ERROR.search(row["error"]):
                        # Het retry ma van loi tam thoi (het quota ngay?): KHONG ghi
                        # checkpoint de lan resume sau chay lai run nay.
                        raise QuotaExhausted(row["error"])
                    _append_checkpoint(ckpt, row)
                    done[key] = row
                    executed += 1
                    mark = "OK " if row["passed"] else ("ERR" if row["error"] else "FAIL")
                    print(f"  [{len(done)}/{total}] {cfg.id} · {case.id} · run {run_idx}"
                          f" -> {mark} ({row['duration_s']}s)")
    except KeyboardInterrupt:
        partial = True
        print("\nBị ngắt (Ctrl+C) — checkpoint đã lưu, chạy lại cùng --name để tiếp tục.")
    except QuotaExhausted as e:
        partial = True
        print(f"\nLỗi tạm thời kéo dài (hết quota?): {e}\n"
              "Run dở KHÔNG ghi checkpoint — chạy lại cùng --name khi quota hồi để tiếp tục.")

    rows = list(done.values())
    summaries = _summaries(rows, configs)
    meta = {
        "timestamp": datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        "name": name,
        "n_cases": len(cases),
        "runs": runs,
        "strategies": strategies,
        "providers": providers,
        "models": {p: _provider_model(p) for p in providers},
        "judge_provider": judge_provider,
        "judge_model": _provider_model(judge_provider) if judge_provider else None,
        "executed": executed,
        "resumed_from_checkpoint": len(rows) - executed,
        "partial": partial,
    }
    _write_report(exp_dir, meta, summaries, rows)
    if charts:
        make_charts(exp_dir, summaries, strategies, providers)
    for leftover in work.glob("*.db*"):
        leftover.unlink(missing_ok=True)
    print(f"\nKết quả: {exp_dir}/report.md")
    return {"meta": meta, "summaries": summaries, "out_dir": str(exp_dir)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Laplace 2x2 experiment runner")
    parser.add_argument("--cases", default="evals/cases")
    parser.add_argument("--strategies", default="react,plan_execute",
                        help="danh sach chien luoc, phay ngan cach")
    parser.add_argument("--providers", default="mock",
                        help="danh sach provider (mock,gemini,openai), phay ngan cach")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default="evals/results")
    parser.add_argument("--name", default=None,
                        help="ten thi nghiem; dung lai ten cu de resume tu checkpoint")
    parser.add_argument("--case-ids", default=None,
                        help="chi chay cac case id nay (phay ngan cach) — dung cho smoke")
    parser.add_argument("--max-case-retries", type=int, default=3)
    parser.add_argument("--backoff-base", type=float, default=20.0)
    parser.add_argument("--no-charts", action="store_true")
    parser.add_argument("--judge", default=None, choices=["mock", "openai", "gemini"],
                        help="provider cham LLM-as-judge cho case co field `judge`")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.case_ids:
        wanted = {s.strip() for s in args.case_ids.split(",") if s.strip()}
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            parser.error(f"case id khong ton tai: {sorted(missing)}")
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    for s in strategies:
        if s not in ("react", "plan_execute"):
            parser.error(f"chien luoc khong hop le: {s}")

    result = run_experiment(
        cases, strategies=strategies, providers=providers, runs=args.runs,
        out_dir=args.out, name=args.name, max_case_retries=args.max_case_retries,
        backoff_base=args.backoff_base, charts=not args.no_charts,
        judge_provider=args.judge)
    print(json.dumps({"meta": result["meta"],
                      "summaries": {k: {m: v.get(m) for m in (*METRIC_KEYS, "runs_total")}
                                    for k, v in result["summaries"].items()}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
