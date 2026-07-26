"""Test error taxonomy + retry/backoff cua tool executor.

Gia lap loi mang/timeout/HTTP bang cach raise exception httpx that tu tool gia,
kiem tra: phan loai dung kind, chi retry loi transient, backoff tang theo cap so
nhan (bat time.sleep bang monkeypatch, khong ngu that).
"""

import time as real_time

import httpx
import pytest
from pydantic import BaseModel

from laplace.schemas import ToolResult
from laplace.tools import base as tools_base
from laplace.tools.base import (
    PermanentToolError,
    ToolContext,
    ToolError,
    TransientToolError,
    _backoff_delay,
    classify_error,
    execute,
    tool,
)


class NoParams(BaseModel):
    pass


@pytest.fixture()
def clean_registry():
    saved = dict(tools_base._REGISTRY)
    tools_base._REGISTRY.clear()
    yield
    tools_base._REGISTRY.clear()
    tools_base._REGISTRY.update(saved)


@pytest.fixture()
def ctx():
    return ToolContext(user_id=1)


@pytest.fixture()
def sleeps(monkeypatch):
    """Ghi lai cac lan backoff sleep thay vi ngu that."""
    recorded: list[float] = []
    monkeypatch.setattr(tools_base.time, "sleep", recorded.append)
    return recorded


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.com/x")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


# ------------------------------------------------------------- classify_error


@pytest.mark.parametrize(
    ("exc", "kind", "transient"),
    [
        (httpx.ConnectError("no route to host"), "network", True),
        (httpx.ReadError("connection reset"), "network", True),
        (httpx.ConnectTimeout("timed out"), "timeout", True),
        (httpx.ReadTimeout("timed out"), "timeout", True),
        (httpx.UnsupportedProtocol("no scheme"), "input", False),
        (httpx.InvalidURL("bad url"), "input", False),
        (ConnectionResetError("reset"), "network", True),
        (TimeoutError("os timeout"), "network", True),
        (RuntimeError("boom"), "crash", True),
        (TransientToolError("x"), "network", True),
        (TransientToolError("x", kind="rate_limit"), "rate_limit", True),
        (PermanentToolError("x"), "tool_error", False),
        (ToolError("x", kind="custom", transient=True), "custom", True),
    ],
)
def test_classify_error_matrix(exc, kind, transient):
    assert classify_error(exc) == (kind, transient)


def test_classify_http_status_codes():
    assert classify_error(_http_error(429)) == ("rate_limit", True)
    assert classify_error(_http_error(500)) == ("http_5xx", True)
    assert classify_error(_http_error(503)) == ("http_5xx", True)
    assert classify_error(_http_error(404)) == ("http_4xx", False)
    assert classify_error(_http_error(403)) == ("http_4xx", False)


# ------------------------------------------------------------ backoff schedule


def test_backoff_delay_grows_exponentially_and_caps():
    assert _backoff_delay(0) == tools_base.BACKOFF_BASE_S
    assert _backoff_delay(1) == tools_base.BACKOFF_BASE_S * 2
    assert _backoff_delay(2) == tools_base.BACKOFF_BASE_S * 4
    assert _backoff_delay(100) == tools_base.BACKOFF_MAX_S


# ---------------------------------------------------- retry loi mang (transient)


def test_network_error_retried_with_backoff_then_recovers(clean_registry, ctx, sleeps):
    calls = {"n": 0}

    @tool("flaky_net", "d", params=NoParams, max_retries=2)
    def flaky_net(params: NoParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("connection refused")
        return ToolResult(ok=True, data="recovered")

    result = execute("flaky_net", {}, ctx)
    assert result.ok is True
    assert result.data == "recovered"
    assert calls["n"] == 3
    # backoff cap so nhan: 0.5s roi 1.0s (theo BACKOFF_BASE_S mac dinh)
    assert sleeps == [_backoff_delay(0), _backoff_delay(1)]
    assert sleeps[1] == sleeps[0] * 2


def test_network_error_exhausts_retries(clean_registry, ctx, sleeps):
    calls = {"n": 0}

    @tool("dead_net", "d", params=NoParams, max_retries=2)
    def dead_net(params: NoParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    result = execute("dead_net", {}, ctx)
    assert result.ok is False
    assert result.error.startswith("[network]")
    assert calls["n"] == 3  # 1 lan dau + 2 retry
    assert len(sleeps) == 2  # khong sleep sau lan cuoi


def test_rate_limit_429_is_retried(clean_registry, ctx, sleeps):
    calls = {"n": 0}

    @tool("limited", "d", params=NoParams, max_retries=1)
    def limited(params: NoParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429)
        return ToolResult(ok=True, data="ok")

    result = execute("limited", {}, ctx)
    assert result.ok is True
    assert calls["n"] == 2
    assert len(sleeps) == 1


# ------------------------------------------------- loi vinh vien: khong retry


def test_http_4xx_not_retried(clean_registry, ctx, sleeps):
    calls = {"n": 0}

    @tool("notfound_http", "d", params=NoParams, max_retries=3)
    def notfound_http(params: NoParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        raise _http_error(404)

    result = execute("notfound_http", {}, ctx)
    assert result.ok is False
    assert result.error.startswith("[http_4xx]")
    assert calls["n"] == 1
    assert sleeps == []


def test_permanent_tool_error_not_retried(clean_registry, ctx, sleeps):
    calls = {"n": 0}

    @tool("perm", "d", params=NoParams, max_retries=3)
    def perm(params: NoParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        raise PermanentToolError("cannot do that")

    result = execute("perm", {}, ctx)
    assert result.ok is False
    assert result.error == "[tool_error] cannot do that"
    assert calls["n"] == 1
    assert sleeps == []


def test_invalid_params_not_retried_and_tool_never_called(clean_registry, ctx, sleeps):
    calls = {"n": 0}

    class StrictParams(BaseModel):
        value: int

    @tool("strict", "d", params=StrictParams, max_retries=3)
    def strict(params: StrictParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        return ToolResult(ok=True)

    result = execute("strict", {"value": "not-an-int"}, ctx)
    assert result.ok is False
    assert result.error.startswith("[input]")
    assert calls["n"] == 0
    assert sleeps == []


def test_unknown_tool_kind_not_found(ctx):
    result = execute("no_such_tool", {}, ctx)
    assert result.ok is False
    assert result.error.startswith("[not_found]")


# ------------------------------------------------------------------- timeout


def test_executor_timeout_not_retried(clean_registry, ctx):
    # Khong dung fixture `sleeps` o day: tool can time.sleep THAT de vuot cutoff.
    calls = {"n": 0}

    @tool("sleepy", "d", params=NoParams, max_retries=3, timeout_s=1)
    def sleepy(params: NoParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        real_time.sleep(2)
        return ToolResult(ok=True)

    result = execute("sleepy", {}, ctx)
    assert result.ok is False
    assert result.error.startswith("[timeout]")
    assert "1s" in result.error
    assert calls["n"] == 1  # khong retry: thread cu co the van chay ngam
