"""Tool registry.

Moi tool dang ky bang decorator @tool voi Pydantic params model. Registry tu
sinh spec (JSON schema) de dua vao prompt cho LLM. Executor validate params,
retry theo cau hinh va tra ve ToolResult; khong bao gio raise ra ngoai.
"""

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from laplace.schemas import ToolResult

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Boi canh runtime truyen vao moi tool."""

    user_id: int
    task_id: int | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    params_model: type[BaseModel]
    fn: Callable[[BaseModel, ToolContext], ToolResult]
    requires_confirmation: bool = False
    timeout_s: int = 15
    max_retries: int = 1
    confirm_when: Callable[[BaseModel], bool] | None = None

    def needs_confirm(self, params: BaseModel) -> bool:
        if self.confirm_when is not None:
            return self.confirm_when(params)
        return self.requires_confirmation

    def llm_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.params_model.model_json_schema(),
            "requires_confirmation": self.requires_confirmation,
        }


_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    name: str,
    description: str,
    params: type[BaseModel],
    *,
    requires_confirmation: bool = False,
    timeout_s: int = 15,
    max_retries: int = 1,
    confirm_when: Callable[[BaseModel], bool] | None = None,
):
    """Decorator dang ky tool. Ham tool nhan (params, ctx) va tra ToolResult."""

    def decorator(fn: Callable[[BaseModel, ToolContext], ToolResult]):
        _REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            params_model=params,
            fn=fn,
            requires_confirmation=requires_confirmation,
            timeout_s=timeout_s,
            max_retries=max_retries,
            confirm_when=confirm_when,
        )
        return fn

    return decorator


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def all_tools() -> dict[str, ToolSpec]:
    return dict(_REGISTRY)


def specs_for_llm() -> list[dict[str, Any]]:
    return [spec.llm_spec() for spec in _REGISTRY.values()]


def clear_registry() -> None:
    """Chi dung trong test."""
    _REGISTRY.clear()


_BUILTIN_MODULES = [
    "fetch_page", "note_store", "report_builder", "scheduler_tool", "task_list", "web_search",
]
_BUILTIN_NAMES = {
    "fetch_page", "note_store", "report_builder", "scheduler", "task_list", "web_search",
}


def load_builtin_tools() -> None:
    """Dang ky 6 tool built-in vao registry.

    Decorator chi chay luc import module lan dau; neu registry da bi clear
    (vi du trong test) thi reload module de dang ky lai.
    """
    import importlib
    import sys

    for mod in _BUILTIN_MODULES:
        importlib.import_module(f"laplace.tools.{mod}")
    if _BUILTIN_NAMES - set(_REGISTRY):
        for mod in _BUILTIN_MODULES:
            importlib.reload(sys.modules[f"laplace.tools.{mod}"])


def execute(name: str, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Thuc thi tool: validate params -> retry loop -> ToolResult. Khong raise."""
    spec = get_tool(name)
    if spec is None:
        return ToolResult(ok=False, error=f"Tool '{name}' khong ton tai")
    try:
        parsed = spec.params_model(**params)
    except ValidationError as e:
        return ToolResult(ok=False, error=f"Tham so khong hop le: {e}")

    last_error = "unknown"
    for attempt in range(spec.max_retries + 1):
        start = time.monotonic()
        try:
            result = _run_with_timeout(spec, parsed, ctx)
            logger.info(
                "tool=%s attempt=%d ok=%s latency_ms=%d",
                name, attempt, result.ok, int((time.monotonic() - start) * 1000),
            )
            if result.ok or attempt == spec.max_retries:
                return result
            last_error = result.error or "tool error"
        except FuturesTimeout:
            # Khong retry khi timeout: thread cu co the van dang chay ngam
            logger.warning("tool=%s timed out after %ds", name, spec.timeout_s)
            return ToolResult(ok=False, error=f"tool '{name}' timed out after {spec.timeout_s}s")
        except Exception as e:  # tool loi bat ngo -> thu lai
            logger.exception("tool=%s attempt=%d crashed", name, attempt)
            last_error = str(e)
    return ToolResult(ok=False, error=last_error)


def _run_with_timeout(spec: ToolSpec, parsed: BaseModel, ctx: ToolContext) -> ToolResult:
    """Chay tool trong worker thread voi cutoff spec.timeout_s.

    Python khong kill duoc thread dang chay: khi timeout, thread cu bi bo lai
    (leak co kiem soat, da log); caller nhan ToolResult timeout ngay lap tuc.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(spec.fn, parsed, ctx).result(timeout=spec.timeout_s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
