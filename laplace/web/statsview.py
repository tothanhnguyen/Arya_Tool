"""Trang thong ke he thong (/stats): hang KPI + 4 chart SVG server-render.

Toan bo hinh hoc (toa do cot, gridline, nhan) tinh san trong route bang Python;
template chi lap qua cac dict — khong can JavaScript. Gom nhom theo NGAY DIA
PHUONG giong traceview, toi da 14 ngay gan nhat co du lieu.
"""

import math
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, func, select

from laplace.db import session_scope
from laplace.models import LLMCall, Step, Task
from laplace.web.deps import require_api_key
from laplace.web.traceview import _day_label, _to_local, templates

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_api_key)])

MAX_DAYS = 14
TOP_TOOLS = 8

# Palette series monochrome (dam -> trung -> nhat). Failed KHONG chi dua vao do
# xam: to bang <pattern> hatch 45 do + legend chu (dinh nghia trong stats.html).
INK = "#0b0b0b"
MID = "#55554f"
LIGHT = "#8a8a85"

# ViewBox chart cot dung (SVG width 100%, responsive)
W, H = 640, 210
PAD_L, PAD_R, PAD_T, PAD_B = 58, 10, 22, 26  # PAD_L du cho nhan "$0.7500"
BAR_R = 4  # bo goc dau cot (chan cot vuong o baseline)
SEG_GAP = 2  # khe giua cac doan stacked

# Chart ngang Top tools
TW = 640
T_PAD_L, T_PAD_R, T_PAD_T, T_PAD_B = 150, 64, 6, 6
ROW_H, HBAR_H = 26, 12


# ------------------------------------------------------------------ dinh dang
def _nice_ceil(v: float) -> float:
    """Tran 'dep' cho truc y: 1/2/2.5/5 x 10^k."""
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        cand = m * 10**exp
        if v <= cand + 1e-12:
            return cand
    return 10.0 ** (exp + 1)


def _fmt_int(v: float) -> str:
    return f"{round(v):,}"


def _fmt_compact(v: float) -> str:
    """Nhan truc gon: 1500 -> 1.5k, 2000000 -> 2M."""
    if v >= 1_000_000:
        s = f"{v / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    if v >= 1_000:
        s = f"{v / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    if v == int(v):
        return str(int(v))
    return f"{v:g}"


def _fmt_cost(v: float) -> str:
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def _fmt_latency(ms: float) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


# ------------------------------------------------------------------ hinh hoc
def _bar_path(x: float, y: float, w: float, h: float, r: float = BAR_R) -> str:
    """Cot dung: dau tren bo tron r, chan vuong o baseline."""
    r = min(r, h, w / 2)
    return (
        f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z"
    )


def _hbar_path(x: float, y: float, w: float, h: float, r: float = BAR_R) -> str:
    """Cot ngang: dau phai bo tron r, chan vuong o truc trai."""
    r = min(r, w, h / 2)
    return (
        f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
        f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z"
    )


def _grid(ymax: float, fmt) -> list[dict]:
    """4 gridline ngang (1/4..4/4 cua ymax)."""
    plot_h = H - PAD_T - PAD_B
    return [
        {"y": round(PAD_T + plot_h * (1 - i / 4), 1), "label": fmt(ymax * i / 4)}
        for i in (1, 2, 3, 4)
    ]


def _x_label(days: list[date], i: int) -> str | None:
    """Thua nhan x: <=9 ngay ghi het, nhieu hon thi cach mot (luon giu ngay cuoi)."""
    if len(days) <= 9 or (len(days) - 1 - i) % 2 == 0:
        return days[i].strftime("%d/%m")
    return None


def _vbar_chart(days: list[date], values: list[float], fmt_val, fmt_axis, unit: str) -> dict:
    """Chart cot dung 1 series, MOT truc y; nhan truc tiep chi o cot max va cot moi nhat."""
    n = len(days)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    ymax = _nice_ceil(max(values, default=0.0))
    slot = plot_w / max(n, 1)
    bar_w = max(3.0, min(slot - SEG_GAP, slot * 0.62, 38.0))
    max_i = values.index(max(values)) if values else -1
    bars = []
    for i, (d, v) in enumerate(zip(days, values)):
        x = PAD_L + slot * i + (slot - bar_w) / 2
        h = plot_h * (v / ymax)
        y = PAD_T + plot_h - h
        bars.append(
            {
                "path": _bar_path(x, y, bar_w, h),
                "title": f"{_day_label(d)}: {fmt_val(v)} {unit}".rstrip(),
                "cx": round(x + bar_w / 2, 1),
                "ly": round(y - 5, 1),
                "label": fmt_val(v) if i in (max_i, n - 1) else None,
                "x_label": _x_label(days, i),
            }
        )
    return {
        "w": W,
        "h": H,
        "grid": _grid(ymax, fmt_axis),
        "baseline": PAD_T + plot_h,
        "pad_l": PAD_L,
        "pad_r": PAD_R,
        "x_label_y": H - 8,
        "bars": bars,
    }


def _stacked_chart(days: list[date], counts: dict[date, list[int]]) -> dict:
    """Chart task theo ngay: stacked 3 doan done/failed/khac, khe 2px giua doan."""
    fills = [INK, "url(#hatch-failed)", LIGHT]
    names = ["done", "failed", "khác"]
    n = len(days)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    totals = [sum(counts[d]) for d in days]
    ymax = _nice_ceil(max(totals, default=0))
    slot = plot_w / max(n, 1)
    bar_w = max(3.0, min(slot - SEG_GAP, slot * 0.62, 38.0))
    max_i = totals.index(max(totals)) if totals else -1
    bars = []
    for i, d in enumerate(days):
        x = PAD_L + slot * i + (slot - bar_w) / 2
        segs, y_bottom = [], PAD_T + plot_h
        parts = [(nm, f, v) for nm, f, v in zip(names, fills, counts[d]) if v > 0]
        for j, (nm, fill, v) in enumerate(parts):
            h = plot_h * (v / ymax)
            gap = SEG_GAP if j > 0 and h > SEG_GAP else 0
            y = y_bottom - h
            top = j == len(parts) - 1
            seg = {
                "fill": fill,
                "title": f"{_day_label(d)} · {nm}: {v} task",
                "shape": "path" if top else "rect",
            }
            # Khe 2px nam DUOI doan (giua doan nay va doan ben duoi)
            if top:
                seg["d"] = _bar_path(x, y, bar_w, h - gap)
            else:
                seg.update(x=round(x, 1), y=round(y, 1), bw=round(bar_w, 1), bh=round(h - gap, 1))
            segs.append(seg)
            y_bottom = y
        bars.append(
            {
                "segments": segs,
                "cx": round(x + bar_w / 2, 1),
                "ly": round(y_bottom - 5, 1),
                "label": str(totals[i]) if i in (max_i, n - 1) else None,
                "x_label": _x_label(days, i),
            }
        )
    return {
        "w": W,
        "h": H,
        "grid": _grid(ymax, _fmt_compact),
        "baseline": PAD_T + plot_h,
        "pad_l": PAD_L,
        "pad_r": PAD_R,
        "x_label_y": H - 8,
        "bars": bars,
    }


def _tools_chart(rows: list[tuple[str, int, int]]) -> dict:
    """Top tools: cot ngang theo so lan goi; phan loi to hatch 45 do o cuoi cot."""
    n = len(rows)
    h = T_PAD_T + n * ROW_H + T_PAD_B
    plot_w = TW - T_PAD_L - T_PAD_R
    vmax = max((calls for _, calls, _ in rows), default=1)
    bars = []
    for i, (tool, calls, errors) in enumerate(rows):
        y = T_PAD_T + i * ROW_H + (ROW_H - HBAR_H) / 2
        w_total = plot_w * calls / vmax
        w_err = plot_w * errors / vmax if errors else 0.0
        w_ok = w_total - w_err - (SEG_GAP if 0 < w_err < w_total else 0)
        segs = []
        if w_ok > 0:
            shape = _hbar_path(T_PAD_L, y, w_ok, HBAR_H) if not w_err else None
            segs.append(
                {
                    "fill": INK,
                    "title": f"{tool}: {calls - errors} lần ok",
                    "shape": "path" if shape else "rect",
                    "d": shape,
                    "x": T_PAD_L,
                    "y": round(y, 1),
                    "bw": round(w_ok, 1),
                    "bh": HBAR_H,
                }
            )
        if w_err:
            segs.append(
                {
                    "fill": "url(#hatch-err)",
                    "title": f"{tool}: {errors} lần lỗi",
                    "shape": "path",
                    "d": _hbar_path(T_PAD_L + w_total - w_err, y, w_err, HBAR_H),
                }
            )
        name = tool if len(tool) <= 18 else tool[:17] + "…"
        value = f"{calls}" + (f" · {errors} lỗi" if errors else "")
        bars.append(
            {
                "name": name,
                "name_y": round(y + HBAR_H - 2, 1),
                "segments": segs,
                "vx": round(T_PAD_L + w_total + 6, 1),
                "value": value,
            }
        )
    return {"w": TW, "h": h, "bars": bars}


# ------------------------------------------------------------------ so lieu
def _collect(session) -> dict:
    tasks = session.execute(select(Task.created_at, Task.status)).all()
    calls = session.execute(
        select(
            LLMCall.created_at,
            LLMCall.prompt_tokens,
            LLMCall.completion_tokens,
            LLMCall.cost_usd,
            LLMCall.latency_ms,
        )
    ).all()
    tool_rows = session.execute(
        select(
            Step.tool,
            func.count(Step.id),
            func.coalesce(func.sum(case((Step.status == "error", 1), else_=0)), 0),
        )
        .group_by(Step.tool)
        .order_by(func.count(Step.id).desc(), Step.tool)
        .limit(TOP_TOOLS)
    ).all()

    # Gom theo ngay dia phuong
    task_by_day: dict[date, list[int]] = {}  # [done, failed, khac]
    for created_at, status in tasks:
        d = _to_local(created_at).date()
        row = task_by_day.setdefault(d, [0, 0, 0])
        idx = 0 if status == "done" else 1 if status == "failed" else 2
        row[idx] += 1
    tok_by_day: dict[date, int] = {}
    cost_by_day: dict[date, float] = {}
    for created_at, p, c, cost, _lat in calls:
        d = _to_local(created_at).date()
        tok_by_day[d] = tok_by_day.get(d, 0) + int(p or 0) + int(c or 0)
        cost_by_day[d] = cost_by_day.get(d, 0.0) + float(cost or 0.0)

    days = sorted(set(task_by_day) | set(tok_by_day))[-MAX_DAYS:]

    # KPI
    n_tasks = len(tasks)
    n_done = sum(1 for _, st in tasks if st == "done")
    total_tokens = sum(int(p or 0) + int(c or 0) for _, p, c, _, _ in calls)
    total_cost = sum(float(cost or 0.0) for _, _, _, cost, _ in calls)
    lats = [int(lat or 0) for _, _, _, _, lat in calls]
    kpis = [
        {"k": "Tổng task", "v": _fmt_int(n_tasks)},
        {"k": "Tỉ lệ done", "v": f"{n_done * 100 / n_tasks:.0f}%" if n_tasks else "—"},
        {"k": "LLM call", "v": _fmt_int(len(calls))},
        {"k": "Tổng token", "v": _fmt_int(total_tokens)},
        {"k": "Tổng cost", "v": _fmt_cost(total_cost)},
        {"k": "Latency LLM TB", "v": _fmt_latency(sum(lats) / len(lats)) if lats else "—"},
    ]

    tokens = [float(tok_by_day.get(d, 0)) for d in days]
    costs = [cost_by_day.get(d, 0.0) for d in days]
    for d in days:
        task_by_day.setdefault(d, [0, 0, 0])

    return {
        "empty": not (tasks or calls or tool_rows),
        "kpis": kpis,
        "token_chart": _vbar_chart(days, tokens, _fmt_int, _fmt_compact, "token"),
        "token_table": [(_day_label(d), _fmt_int(t)) for d, t in zip(days, tokens)],
        "cost_chart": _vbar_chart(days, costs, _fmt_cost, _fmt_cost, ""),
        "cost_table": [(_day_label(d), _fmt_cost(c)) for d, c in zip(days, costs)],
        "task_chart": _stacked_chart(days, task_by_day),
        "task_table": [
            (_day_label(d), *task_by_day[d], sum(task_by_day[d])) for d in days
        ],
        "tools_chart": _tools_chart([(t, int(n), int(e)) for t, n, e in tool_rows]),
        "tools_table": [(t, int(n), int(e)) for t, n, e in tool_rows],
    }


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    with session_scope() as session:
        data = _collect(session)
    return templates.TemplateResponse(request, "stats.html", {**data, "active_nav": "stats"})
