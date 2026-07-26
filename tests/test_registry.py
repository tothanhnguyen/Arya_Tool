"""Test tool registry: dang ky, validate, retry, confirm — dung tool gia."""

import pytest
from pydantic import BaseModel

from laplace.schemas import ToolResult
from laplace.tools import base as tools_base
from laplace.tools.base import (
    ToolContext,
    all_tools,
    execute,
    get_tool,
    specs_for_llm,
    tool,
)


class FakeParams(BaseModel):
    value: int
    mode: str = "normal"


@pytest.fixture()
def clean_registry():
    """Cach ly registry: xoa truoc test, khoi phuc nguyen trang sau test."""
    saved = dict(tools_base._REGISTRY)
    tools_base._REGISTRY.clear()
    yield
    tools_base._REGISTRY.clear()
    tools_base._REGISTRY.update(saved)


@pytest.fixture()
def ctx():
    return ToolContext(user_id=1)


def test_decorator_registers_tool(clean_registry, ctx):
    @tool("fake_tool", "A fake tool for tests", params=FakeParams)
    def fake_tool(params: FakeParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data=params.value * 2)

    assert "fake_tool" in all_tools()

    specs = specs_for_llm()
    spec = next(s for s in specs if s["name"] == "fake_tool")
    assert spec["description"] == "A fake tool for tests"
    assert "value" in spec["parameters"]["properties"]
    assert spec["requires_confirmation"] is False

    result = execute("fake_tool", {"value": 21}, ctx)
    assert result.ok is True
    assert result.data == 42


def test_execute_invalid_params_returns_not_ok(clean_registry, ctx):
    @tool("fake_tool", "d", params=FakeParams)
    def fake_tool(params: FakeParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True)

    result = execute("fake_tool", {"value": "definitely-not-an-int"}, ctx)
    assert result.ok is False
    assert result.error


def test_execute_unknown_tool_returns_not_ok(clean_registry, ctx):
    result = execute("does_not_exist_xyz", {}, ctx)
    assert result.ok is False
    assert "does_not_exist_xyz" in (result.error or "")


def test_retry_succeeds_on_second_attempt(clean_registry, ctx):
    calls = {"n": 0}

    @tool("flaky", "d", params=FakeParams, max_retries=1)
    def flaky(params: FakeParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolResult(ok=False, error="transient failure")
        return ToolResult(ok=True, data="recovered")

    result = execute("flaky", {"value": 1}, ctx)
    assert result.ok is True
    assert result.data == "recovered"
    assert calls["n"] == 2


def test_retry_on_exception_does_not_raise(clean_registry, ctx):
    calls = {"n": 0}

    @tool("crashy", "d", params=FakeParams, max_retries=1)
    def crashy(params: FakeParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return ToolResult(ok=True, data="ok after crash")

    result = execute("crashy", {"value": 1}, ctx)
    assert result.ok is True
    assert calls["n"] == 2


def test_retry_exhausted_returns_last_error(clean_registry, ctx):
    calls = {"n": 0}

    @tool("always_fail", "d", params=FakeParams, max_retries=1)
    def always_fail(params: FakeParams, ctx: ToolContext) -> ToolResult:
        calls["n"] += 1
        return ToolResult(ok=False, error=f"failure #{calls['n']}")

    result = execute("always_fail", {"value": 1}, ctx)
    assert result.ok is False
    assert calls["n"] == 2  # max_retries=1 -> 2 lan thu


def test_needs_confirm_with_confirm_when(clean_registry):
    @tool("cond", "d", params=FakeParams, confirm_when=lambda p: p.mode == "danger")
    def cond(params: FakeParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True)

    spec = get_tool("cond")
    assert spec is not None
    assert spec.needs_confirm(FakeParams(value=1, mode="danger")) is True
    assert spec.needs_confirm(FakeParams(value=1, mode="normal")) is False


def test_needs_confirm_default_flag(clean_registry):
    @tool("writer", "d", params=FakeParams, requires_confirmation=True)
    def writer(params: FakeParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True)

    spec = get_tool("writer")
    assert spec is not None
    assert spec.needs_confirm(FakeParams(value=1)) is True
