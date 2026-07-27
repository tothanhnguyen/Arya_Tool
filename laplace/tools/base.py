"""Tool registry.

Moi tool dang ky bang decorator @tool voi Pydantic params model. Registry tu
sinh spec (JSON schema) de dua vao prompt cho LLM. Executor validate params,
retry theo cau hinh va tra ve ToolResult; khong bao gio raise ra ngoai.

Error taxonomy (chuoi error co prefix "[kind]" de LLM va nguoi doc trace
phan biet nhanh):
- [input]      tham so sai schema / URL khong hop le  -> KHONG retry
- [not_found]  tool khong ton tai                     -> KHONG retry
- [timeout]    tool vuot cutoff timeout_s             -> KHONG retry (thread cu con chay ngam)
- [network]    loi mang tam thoi (connect/read...)    -> retry + backoff
- [rate_limit] HTTP 429                               -> retry + backoff
- [http_5xx]   server loi tam thoi                    -> retry + backoff
- [http_4xx]   loi phia client (404, 403...)          -> KHONG retry
- [crash]      exception bat ngo trong tool           -> retry + backoff (1 co hoi)
- [tool_error] tool chu dong tra ok=False             -> KHONG retry (loi nghiep vu)

Tool co the chu dong phan loai bang cach raise TransientToolError /
PermanentToolError; exception khac duoc `classify_error` suy ra tu loai
(httpx timeout/network/status...).
"""

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from laplace.schemas import ToolResult

logger = logging.getLogger(__name__)

# Exponential backoff giua cac lan retry loi tam thoi: base * 2^attempt, co tran.
BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0


class ToolError(Exception):
    """Loi tool co phan loai ro rang (kind + transient)."""

    def __init__(self, message: str, *, kind: str = "tool_error", transient: bool = False):
        super().__init__(message)
        self.kind = kind
        self.transient = transient


class TransientToolError(ToolError):
    """Loi tam thoi (mang, 5xx, rate limit) -> executor retry voi backoff."""

    def __init__(self, message: str, *, kind: str = "network"):
        super().__init__(message, kind=kind, transient=True)


class PermanentToolError(ToolError):
    """Loi vinh vien (input sai, khong co quyen...) -> tra ve ngay, khong retry."""

    def __init__(self, message: str, *, kind: str = "tool_error"):
        super().__init__(message, kind=kind, transient=False)


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """Phan loai exception -> (kind, transient). Transient = dang retry."""
    if isinstance(exc, ToolError):
        return exc.kind, exc.transient
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return "rate_limit", True
        if code >= 500:
            return "http_5xx", True
        return "http_4xx", False
    if isinstance(exc, httpx.UnsupportedProtocol | httpx.InvalidURL):
        return "input", False
    if isinstance(exc, httpx.TransportError):  # ConnectError, ReadError, ProxyError...
        return "network", True
    if isinstance(exc, ConnectionError | TimeoutError):
        return "network", True
    return "crash", True  # loi bat ngo: cho retry nhu hanh vi cu


def _backoff_delay(attempt: int) -> float:
    """Do tre truoc lan retry thu `attempt + 1` (attempt dem tu 0)."""
    return min(BACKOFF_BASE_S * (2**attempt), BACKOFF_MAX_S)


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
    "fetch_page",
    "note_store",
    "report_builder",
    "scheduler_tool",
    "social_account",
    "social_content",
    "social_schedule",
    "task_list",
    "web_search",
]
_BUILTIN_NAMES = {
    "fetch_page",
    "note_store",
    "report_builder",
    "scheduler",
    "social_account",
    "social_content",
    "social_schedule",
    "task_list",
    "web_search",
}


def load_builtin_tools() -> None:
    """Dang ky cac tool built-in vao registry.

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
    """Thuc thi tool: validate params -> retry loop (chi loi transient) -> ToolResult.

    Khong bao gio raise. Chuoi error co prefix "[kind]" theo taxonomy o dau file.
    """
    spec = get_tool(name)
    if spec is None:
        return ToolResult(ok=False, error=f"[not_found] Tool '{name}' khong ton tai")
    try:
        parsed = spec.params_model(**params)
    except ValidationError as e:
        return ToolResult(ok=False, error=f"[input] Tham so khong hop le: {e}")

    last_error = "[crash] unknown"
    for attempt in range(spec.max_retries + 1):
        start = time.monotonic()
        try:
            result = _run_with_timeout(spec, parsed, ctx)
        except FuturesTimeout:
            # Khong retry khi timeout: thread cu co the van dang chay ngam
            logger.warning("tool=%s timed out after %ds", name, spec.timeout_s)
            return ToolResult(
                ok=False, error=f"[timeout] tool '{name}' timed out after {spec.timeout_s}s"
            )
        except Exception as e:
            kind, transient = classify_error(e)
            last_error = f"[{kind}] {e}"
            logger.warning(
                "tool=%s attempt=%d error kind=%s transient=%s: %s",
                name, attempt, kind, transient, e,
                exc_info=kind == "crash",
            )
            if not transient or attempt == spec.max_retries:
                return ToolResult(ok=False, error=last_error)
            time.sleep(_backoff_delay(attempt))
            continue
        logger.info(
            "tool=%s attempt=%d ok=%s latency_ms=%d",
            name, attempt, result.ok, int((time.monotonic() - start) * 1000),
        )
        if result.ok or result.error and result.error.startswith("["):
            return result
        # Tool chu dong tra ok=False = loi nghiep vu (permanent) -> khong retry,
        # chi gan prefix taxonomy neu tool chua tu phan loai.
        return ToolResult(ok=False, data=result.data, error=f"[tool_error] {result.error or ''}")
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
