"""Test trang xem ket qua eval (/evals): list, detail 2 loai, raw, path safety."""

import json

import pytest
from fastapi.testclient import TestClient

from laplace.web import evalsview

# results.json toi gian dung schema that (run don: meta/summary/runs)
RUN_RESULTS = {
    "meta": {"timestamp": "20260101-120000", "provider": "mock", "runs": 1, "n_cases": 2},
    "summary": {
        "runs_total": 2,
        "success_rate": 0.5,
        "route_accuracy": 1.0,
        "recovery_rate": None,
        "avg_steps": 1.5,
        "avg_tokens": 60,
        "failures": [
            {"id": "case_b", "run": 0, "error": "boom", "failed_checks": ["route", "tools"]}
        ],
    },
    "runs": [
        {"id": "case_a", "run": 0, "status": "done", "passed": True, "error": None},
        {"id": "case_b", "run": 0, "status": "failed", "passed": False, "error": "boom"},
    ],
}

# results.json toi gian dung schema experiment (meta/summaries theo cau hinh)
EXP_RESULTS = {
    "meta": {
        "timestamp": "20260102-090000",
        "name": "exp-demo",
        "n_cases": 2,
        "runs": 2,
        "strategies": ["react", "plan_execute"],
        "providers": ["mock"],
        "models": {"mock": "mock"},
    },
    "summaries": {
        "react__mock": {
            "runs_total": 4,
            "success_rate": 0.75,
            "avg_steps": 1.2,
            "failures": [
                {"id": "case_x", "run": 1, "error": None, "failed_checks": ["tools"]}
            ],
        },
        "plan_execute__mock": {
            "runs_total": 4,
            "success_rate": 1.0,
            "avg_steps": 2.0,
            "failures": [],
        },
    },
}

RUN_NAME = "20260101-120000-mock"
EXP_NAME = "exp-demo"


@pytest.fixture()
def eval_dirs(tmp_path, monkeypatch):
    """Cay thu muc gia: 1 run don + 1 experiment + 1 thu muc khong co results.json."""
    run_base = tmp_path / "eval_results"
    exp_base = tmp_path / "evals" / "results"

    run_dir = run_base / RUN_NAME
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text(json.dumps(RUN_RESULTS), encoding="utf-8")

    exp_dir = exp_base / EXP_NAME
    exp_dir.mkdir(parents=True)
    (exp_dir / "results.json").write_text(json.dumps(EXP_RESULTS), encoding="utf-8")

    (run_base / "khong-co-results").mkdir()  # phai duoc bo qua em

    monkeypatch.setattr(evalsview, "EVAL_DIRS", {"results": run_base, "exp": exp_base})
    return {"results": run_base, "exp": exp_base}


@pytest.fixture()
def client(session, eval_dirs):
    from laplace.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def test_list_shows_both_sources_sorted(client):
    r = client.get("/evals")
    assert r.status_code == 200
    assert RUN_NAME in r.text
    assert EXP_NAME in r.text
    assert "run đơn" in r.text
    assert "experiment" in r.text
    assert "2 cấu hình" in r.text  # experiment: so cau hinh thay cho success_rate
    assert "50.0%" in r.text  # run don: success_rate dang %
    assert "khong-co-results" not in r.text  # thu muc thieu results.json bi bo qua
    # sort moi nhat truoc: experiment (20260102) dung truoc run don (20260101)
    assert r.text.index(EXP_NAME) < r.text.index(RUN_NAME)


def test_run_detail(client):
    r = client.get(f"/evals/results/{RUN_NAME}")
    assert r.status_code == 200
    assert "success_rate" in r.text
    assert "50.0%" in r.text  # ti le hien thi % 1 chu so thap phan
    assert "100.0%" in r.text  # route_accuracy
    assert "1.5" in r.text  # avg_steps khong bi doi thanh %
    assert 'class="meter"' in r.text  # thanh meter cho metric ti le
    # failures: id, run, failed_checks, error
    assert "case_b" in r.text
    assert "route, tools" in r.text
    assert "boom" in r.text
    assert f"/evals/results/{RUN_NAME}/raw" in r.text  # link tai raw JSON


def test_exp_detail(client):
    r = client.get(f"/evals/exp/{EXP_NAME}")
    assert r.status_code == 200
    assert "react__mock" in r.text
    assert "plan_execute__mock" in r.text
    assert "75.0%" in r.text
    assert "100.0%" in r.text
    assert "case_x" in r.text  # failure cua config react__mock


def test_raw_download(client):
    r = client.get(f"/evals/results/{RUN_NAME}/raw")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]
    assert json.loads(r.content) == RUN_RESULTS


def test_path_traversal_and_unknown_404(client, eval_dirs):
    # ../ ma hoa URL khong duoc di lot ra ngoai base dir
    secret = eval_dirs["results"].parent / "secret"
    secret.mkdir()
    (secret / "results.json").write_text("{}", encoding="utf-8")
    assert client.get("/evals/results/..%2f..%2fetc").status_code == 404
    assert client.get("/evals/results/..%2fsecret").status_code == 404
    assert client.get("/evals/results/khong-ton-tai").status_code == 404
    assert client.get("/evals/bogus/exp-demo").status_code == 404
    assert client.get("/evals/results/ten%20la!").status_code == 404
    assert client.get("/evals/results/..%2f..%2fetc/raw").status_code == 404


def test_empty_dirs_show_empty_state(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        evalsview,
        "EVAL_DIRS",
        {"results": tmp_path / "trong-a", "exp": tmp_path / "trong-b"},
    )
    r = client.get("/evals")
    assert r.status_code == 200
    assert "Chưa có kết quả eval" in r.text
