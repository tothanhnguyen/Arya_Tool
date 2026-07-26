"""Test search cache cho web_search (T7 — PLAN.md muc 8.8).

Khong goi mang: httpx.post duoc monkeypatch bang fake dem so lan goi.
Cache bat/tat qua env LAPLACE_SEARCH_CACHE (doc truc tiep, khong qua config).
"""

import httpx
import pytest

from laplace.config import get_settings
from laplace.tools import web_search as ws
from laplace.tools.base import ToolContext, execute, load_builtin_tools

load_builtin_tools()


@pytest.fixture()
def ctx():
    return ToolContext(user_id=1)


@pytest.fixture(autouse=True)
def _fresh_cache():
    ws.clear_search_cache()
    yield
    ws.clear_search_cache()


@pytest.fixture()
def fake_api(monkeypatch):
    """Gia lap Tavily API: dem so lan goi, tra payload phu thuoc counter."""
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        payload = {
            "results": [
                {
                    "title": f"result for {json['query']} (call #{calls['n']})",
                    "url": "https://example.com/a",
                    "content": "snippet",
                }
            ]
        }
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json=payload)

    monkeypatch.setattr(ws.httpx, "post", fake_post)
    monkeypatch.setattr(get_settings(), "search_api_key", "fake-key")
    return calls


@pytest.fixture()
def cache_on(monkeypatch):
    monkeypatch.setenv("LAPLACE_SEARCH_CACHE", "1")
    monkeypatch.delenv("LAPLACE_SEARCH_CACHE_TTL_S", raising=False)


# ------------------------------------------------------------------ tat/bat


def test_cache_disabled_by_default(fake_api, ctx, monkeypatch):
    monkeypatch.delenv("LAPLACE_SEARCH_CACHE", raising=False)
    r1 = execute("web_search", {"query": "fastapi vs flask"}, ctx)
    r2 = execute("web_search", {"query": "fastapi vs flask"}, ctx)
    assert r1.ok and r2.ok
    assert fake_api["n"] == 2  # khong cache -> goi API 2 lan
    assert r1.data != r2.data  # payload doi theo counter


def test_cache_hit_same_observation_single_api_call(fake_api, ctx, cache_on):
    r1 = execute("web_search", {"query": "fastapi vs flask"}, ctx)
    r2 = execute("web_search", {"query": "fastapi vs flask"}, ctx)
    assert r1.ok and r2.ok
    assert fake_api["n"] == 1  # lan 2 la cache hit
    assert r1.data == r2.data  # cung input -> cung observation (eval on dinh)


def test_cache_key_normalizes_case_and_whitespace(fake_api, ctx, cache_on):
    r1 = execute("web_search", {"query": "FastAPI  vs   Flask"}, ctx)
    r2 = execute("web_search", {"query": "  fastapi vs flask "}, ctx)
    assert fake_api["n"] == 1
    assert r1.data == r2.data


def test_different_max_results_is_different_entry(fake_api, ctx, cache_on):
    execute("web_search", {"query": "abc", "max_results": 3}, ctx)
    execute("web_search", {"query": "abc", "max_results": 5}, ctx)
    assert fake_api["n"] == 2


def test_cached_data_is_isolated_copy(fake_api, ctx, cache_on):
    r1 = execute("web_search", {"query": "abc"}, ctx)
    r1.data["results"][0]["title"] = "TAMPERED"
    r2 = execute("web_search", {"query": "abc"}, ctx)
    assert r2.data["results"][0]["title"] != "TAMPERED"


# ------------------------------------------------------------------ TTL/loi


def test_cache_expires_after_ttl(fake_api, ctx, cache_on, monkeypatch):
    monkeypatch.setenv("LAPLACE_SEARCH_CACHE_TTL_S", "100")
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ws.time, "monotonic", lambda: fake_now["t"])

    execute("web_search", {"query": "abc"}, ctx)
    fake_now["t"] += 99
    execute("web_search", {"query": "abc"}, ctx)
    assert fake_api["n"] == 1  # chua het TTL -> hit

    fake_now["t"] += 2  # tong 101s -> het han
    execute("web_search", {"query": "abc"}, ctx)
    assert fake_api["n"] == 2


def test_api_error_is_not_cached(ctx, cache_on, monkeypatch):
    calls = {"n": 0}

    def flaky_post(url, json=None, timeout=None):
        calls["n"] += 1
        req = httpx.Request("POST", url)
        if calls["n"] == 1:
            raise httpx.ConnectError("network down")
        return httpx.Response(
            200, request=req, json={"results": [{"title": "t", "url": "u", "content": "c"}]}
        )

    monkeypatch.setattr(ws.httpx, "post", flaky_post)
    monkeypatch.setattr(get_settings(), "search_api_key", "fake-key")
    monkeypatch.setattr("laplace.tools.base.BACKOFF_BASE_S", 0.0)

    # Lan 1 loi mang -> executor retry -> lan 2 thanh cong; loi khong vao cache
    result = execute("web_search", {"query": "abc"}, ctx)
    assert result.ok is True
    assert calls["n"] == 2

    # Goi lai: phai la cache hit tu ket qua THANH CONG
    result2 = execute("web_search", {"query": "abc"}, ctx)
    assert calls["n"] == 2
    assert result2.data == result.data


def test_stub_mode_ignores_cache(ctx, cache_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "search_api_key", None)
    r1 = execute("web_search", {"query": "abc"}, ctx)
    r2 = execute("web_search", {"query": "abc"}, ctx)
    assert r1.ok and r2.ok
    assert r1.data["stub"] is True
    assert r1.data == r2.data  # stub von da deterministic
    assert len(ws._cache) == 0  # khong ghi entry nao vao cache


# ------------------------------------------------------------------ eviction


def test_cache_evicts_oldest_when_full(fake_api, ctx, cache_on, monkeypatch):
    monkeypatch.setattr(ws, "_CACHE_MAX_ENTRIES", 2)
    execute("web_search", {"query": "q1"}, ctx)
    execute("web_search", {"query": "q2"}, ctx)
    execute("web_search", {"query": "q3"}, ctx)  # day q1 ra
    assert fake_api["n"] == 3
    assert len(ws._cache) == 2

    execute("web_search", {"query": "q3"}, ctx)  # van hit
    assert fake_api["n"] == 3
    execute("web_search", {"query": "q1"}, ctx)  # da bi evict -> goi lai API
    assert fake_api["n"] == 4
