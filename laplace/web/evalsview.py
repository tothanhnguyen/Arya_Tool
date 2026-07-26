"""Trang xem ket qua eval (/evals): run don trong eval_results/ va
experiment trong evals/results/. Chi doc file results.json, khong ghi gi."""

import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from laplace.web.deps import require_api_key

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_api_key)])

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 2 nguon ket qua eval; test monkeypatch dict nay de tro vao cay thu muc gia.
EVAL_DIRS: dict[str, Path] = {
    "results": _PROJECT_ROOT / "eval_results",  # run don
    "exp": _PROJECT_ROOT / "evals" / "results",  # experiment
}

# Ten thu muc hop le: khong cho "/" hay byte la nao di qua (chong path traversal).
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ------------------------------------------------------------------ helpers
def _results_path(source: str, name: str) -> Path:
    """Duong dan results.json da validate. Moi truong hop sai deu tra 404
    (khong lo thong tin thu muc ton tai hay khong)."""
    base = EVAL_DIRS.get(source)
    if base is None or not _NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="Khong tim thay ket qua eval")
    base = base.resolve()
    target = (base / name).resolve()
    if target == base or not target.is_relative_to(base):
        raise HTTPException(status_code=404, detail="Khong tim thay ket qua eval")
    results = target / "results.json"
    if not results.is_file():
        raise HTTPException(status_code=404, detail="Khong tim thay ket qua eval")
    return results


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=404, detail="results.json khong doc duoc") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=404, detail="results.json sai dinh dang")
    return data


def _is_ratio(key: str) -> bool:
    """Metric dang ti le 0..1 (hien thi % + thanh meter)."""
    return key.endswith(("_rate", "_accuracy"))


def _fmt_num(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, float):
        return str(value)
    return f"{value:g}"


def _metric_cell(key: str, value) -> dict:
    """{display, pct}: pct != None => ve thanh meter 0..100."""
    if value is None:
        return {"display": "—", "pct": None}
    if _is_ratio(key) and isinstance(value, (int, float)):
        pct = max(0.0, min(float(value), 1.0)) * 100
        return {"display": f"{float(value) * 100:.1f}%", "pct": round(pct, 1)}
    if isinstance(value, (int, float)):
        return {"display": _fmt_num(value), "pct": None}
    return {"display": str(value), "pct": None}


def _summary_metrics(summary: dict) -> list[dict]:
    """[{name, display, pct}] — bo qua danh sach failures."""
    return [
        {"name": key, **_metric_cell(key, value)}
        for key, value in summary.items()
        if key != "failures"
    ]


def _list_row(source: str, name: str, data: dict) -> dict:
    meta = data.get("meta") or {}
    if "summaries" in data:  # experiment: nhieu cau hinh trong "summaries"
        providers = ", ".join(meta.get("providers") or []) or "—"
        result = f"{len(data['summaries'])} cấu hình"
        kind = "experiment"
    else:  # run don: mot "summary" duy nhat
        providers = meta.get("provider") or "—"
        rate = (data.get("summary") or {}).get("success_rate")
        result = f"{rate * 100:.1f}%" if isinstance(rate, (int, float)) else "—"
        kind = "run"
    return {
        "source": source,
        "name": name,
        "kind": kind,
        "timestamp": meta.get("timestamp") or "—",
        "providers": providers,
        "n_cases": meta.get("n_cases", "—"),
        "runs": meta.get("runs", "—"),
        "result": result,
    }


def _scan_rows() -> list[dict]:
    rows: list[dict] = []
    for source, base in EVAL_DIRS.items():
        if not base.is_dir():
            continue
        for child in base.iterdir():
            results = child / "results.json"
            if not child.is_dir() or not results.is_file():
                continue  # thu muc khong co results.json: bo qua em
            try:
                data = json.loads(results.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                rows.append(_list_row(source, child.name, data))
    rows.sort(key=lambda r: (r["timestamp"], r["name"]), reverse=True)
    return rows


# ------------------------------------------------------------------ routes
@router.get("/evals", response_class=HTMLResponse)
def evals_list(request: Request):
    return templates.TemplateResponse(
        request,
        "evals_list.html",
        {"rows": _scan_rows(), "active_nav": "evals"},
    )


@router.get("/evals/{source}/{name}/raw")
def eval_raw(source: str, name: str):
    path = _results_path(source, name)
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{name}-results.json",
        content_disposition_type="attachment",
    )


@router.get("/evals/{source}/{name}", response_class=HTMLResponse)
def eval_detail(request: Request, source: str, name: str):
    data = _load(_results_path(source, name))
    meta = data.get("meta") or {}
    ctx: dict = {
        "name": name,
        "source": source,
        "meta": meta,
        "raw_url": f"/evals/{source}/{name}/raw",
        "active_nav": "evals",
    }

    if "summaries" in data:  # experiment: so sanh cac cau hinh
        summaries: dict = data["summaries"]
        configs = list(summaries.keys())
        metric_keys: list[str] = []
        for summary in summaries.values():
            for key in summary:
                if key != "failures" and key not in metric_keys:
                    metric_keys.append(key)
        metric_rows = [
            {
                "name": key,
                "cells": [
                    _metric_cell(key, (summaries[cfg] or {}).get(key))
                    for cfg in configs
                ],
            }
            for key in metric_keys
        ]
        config_failures = [
            {"config": cfg, "failures": (summaries[cfg] or {}).get("failures") or []}
            for cfg in configs
            if (summaries[cfg] or {}).get("failures")
        ]
        ctx.update(
            {
                "kind": "experiment",
                "configs": configs,
                "metric_rows": metric_rows,
                "config_failures": config_failures,
            }
        )
    else:  # run don
        summary = data.get("summary") or {}
        ctx.update(
            {
                "kind": "run",
                "metrics": _summary_metrics(summary),
                "failures": summary.get("failures") or [],
            }
        )
    return templates.TemplateResponse(request, "evals_detail.html", ctx)
