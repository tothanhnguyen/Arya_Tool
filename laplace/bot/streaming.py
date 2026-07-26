"""Streaming status: bao tien do tung buoc cua agent ve Telegram.

Cach hoat dong (khong dung vao laplace/agent/*):
- `run_task` / `resume_task` nhan tham so `llm` inject duoc. Bot boc provider
  that bang `ProgressLLM`: moi lan orchestrator goi `complete()`, wrapper suy ra
  giai doan hien tai (classify / suy nghi / chay tool X / tong hop...) tu
  json_schema cua call va noi dung structured output tra ve, roi phat
  `ProgressEvent`.
- `ProgressStreamer` nhan event tu worker thread (qua call_soon_threadsafe +
  asyncio.Queue), gom lai va EDIT mot message trang thai duy nhat tren Telegram
  (throttle de khong dinh rate-limit edit). Khi task xong, handler goi
  `finalize()` de thay message trang thai bang ket qua cuoi (hoac text confirm).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from laplace.llm.base import LLMProvider, LLMResult, get_provider

logger = logging.getLogger(__name__)

T = TypeVar("T")

_STOP = object()  # sentinel dung consumer


@dataclass(frozen=True)
class ProgressEvent:
    """Mot giai doan tien do: emoji + nhan (viet thuong, vd 'tìm kiếm web')."""

    emoji: str
    label: str

    @property
    def doing(self) -> str:
        return f"{self.emoji} Đang {self.label}…"

    @property
    def done(self) -> str:
        return f"✅ {self.label[:1].upper()}{self.label[1:]}"


CLASSIFY = ProgressEvent("🧭", "phân loại yêu cầu")
THINK = ProgressEvent("🤔", "suy nghĩ bước tiếp theo")
PLAN = ProgressEvent("📋", "lập kế hoạch")
EVALUATE = ProgressEvent("🧪", "đánh giá kết quả bước")
REPLAN = ProgressEvent("🔁", "điều chỉnh kế hoạch")
COMPOSE = ProgressEvent("✍️", "tổng hợp câu trả lời")

TOOL_EVENTS: dict[str, ProgressEvent] = {
    "web_search": ProgressEvent("🔍", "tìm kiếm web"),
    "fetch_page": ProgressEvent("🌐", "tải trang web"),
    "note_store": ProgressEvent("🗒️", "thao tác ghi chú"),
    "task_list": ProgressEvent("📝", "cập nhật việc cần làm"),
    "scheduler": ProgressEvent("⏰", "thao tác lịch chạy"),
    "report_builder": ProgressEvent("📊", "dựng báo cáo"),
}

INITIAL_TEXT = "⏳ Đang xử lý yêu cầu của bạn…"


def tool_event(name: str) -> ProgressEvent:
    return TOOL_EVENTS.get(name) or ProgressEvent(
        "🔧", f"chạy công cụ {name}".strip()
    )


class ProgressLLM:
    """Boc mot LLMProvider, phat ProgressEvent quanh moi lan complete().

    Suy ra giai doan tu json_schema (classify/react/plan/evaluate) truoc khi
    goi, va tu structured output sau khi goi (tool sap chay, plan step ke tiep).
    Chay tren worker thread — callback `emit` phai thread-safe.
    """

    def __init__(self, inner: LLMProvider, emit: Callable[[ProgressEvent], None]):
        self._inner = inner
        self._emit = emit
        self._plan_tools: list[str] = []
        self._plan_next = 0

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "unknown")

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        self._emit_before(json_schema)
        result = self._inner.complete(messages, json_schema=json_schema)
        self._emit_after(result.parsed)
        return result

    def _safe_emit(self, event: ProgressEvent) -> None:
        try:
            self._emit(event)
        except Exception:  # tien do chi la cosmetic — khong duoc lam hong task
            logger.debug("emit progress event that bai", exc_info=True)

    def _emit_before(self, json_schema: dict[str, Any] | None) -> None:
        if json_schema is None:
            self._safe_emit(COMPOSE)  # cau tra loi direct/clarify/final
            return
        props = json_schema.get("properties", {})
        if "route" in props:
            self._safe_emit(CLASSIFY)
        elif "action" in props:
            self._safe_emit(THINK)
        elif "steps" in props:
            self._safe_emit(PLAN)
        elif "decision" in props:
            self._safe_emit(EVALUATE)

    def _emit_after(self, parsed: Any) -> None:
        if not isinstance(parsed, dict):
            return
        if parsed.get("action") == "tool":
            # ReAct: buoc tiep theo cua orchestrator la execute tool nay
            self._safe_emit(tool_event(str(parsed.get("tool") or "")))
        elif isinstance(parsed.get("steps"), list):
            # Plan moi: buoc dau tien sap duoc thuc thi
            self._plan_tools = [
                str(s.get("tool", ""))
                for s in parsed["steps"]
                if isinstance(s, dict)
            ]
            self._plan_next = 0
            self._emit_plan_step()
        elif "decision" in parsed:
            decision = parsed.get("decision")
            if decision == "continue":
                self._plan_next += 1
                self._emit_plan_step()
            elif decision == "replan":
                self._safe_emit(REPLAN)

    def _emit_plan_step(self) -> None:
        if 0 <= self._plan_next < len(self._plan_tools):
            self._safe_emit(tool_event(self._plan_tools[self._plan_next]))


class ProgressStreamer:
    """Giu MOT message trang thai tren Telegram va edit no theo tien do.

    status_message: message bot da gui (co coroutine `edit_text`).
    """

    def __init__(
        self,
        status_message: Any,
        *,
        min_interval: float = 1.5,
        max_lines: int = 8,
    ):
        self._status = status_message
        self._min_interval = min_interval
        self._max_lines = max_lines
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._done_lines: list[str] = []
        self._current: ProgressEvent | None = None
        self._last_text = INITIAL_TEXT
        self._last_edit = 0.0
        self._finalized = False

    @classmethod
    async def start(cls, message: Any, **kwargs: Any) -> "ProgressStreamer":
        """Gui message trang thai ban dau duoi `message` va tao streamer."""
        status = await message.answer(INITIAL_TEXT)
        return cls(status, **kwargs)

    def wrap(self, llm: LLMProvider | None = None) -> ProgressLLM:
        """Boc provider (mac dinh: provider theo config) de phat tien do."""
        return ProgressLLM(llm or get_provider(), self._emit_threadsafe)

    async def run(
        self, worker: Callable[[LLMProvider], T], llm: LLMProvider | None = None
    ) -> T:
        """Chay `worker(wrapped_llm)` trong thread, stream tien do trong luc cho.

        worker la ham SYNC, vd: lambda p: run_task(task_id, llm=p).
        """
        self._loop = asyncio.get_running_loop()
        wrapped = self.wrap(llm)
        consumer = asyncio.create_task(self._consume())
        try:
            return await asyncio.to_thread(worker, wrapped)
        finally:
            # Nhuong loop mot nhip de cac callback call_soon_threadsafe (emit
            # tu worker thread) con pending duoc flush vao queue truoc _STOP.
            await asyncio.sleep(0)
            self._queue.put_nowait(_STOP)
            await consumer

    async def finalize(self, text: str, reply_markup: Any = None) -> bool:
        """Thay message trang thai bang noi dung cuoi. True neu edit thanh cong."""
        self._finalized = True
        text = text or "(không có nội dung)"
        try:
            await self._status.edit_text(text, reply_markup=reply_markup)
            return True
        except Exception:
            logger.warning("edit message trang thai that bai", exc_info=True)
            return False

    # --- noi bo ---

    def _emit_threadsafe(self, event: ProgressEvent) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _apply(self, event: ProgressEvent) -> None:
        if event == self._current:
            return  # trung giai doan (vd retry schema) — bo qua
        if self._current is not None:
            self._done_lines.append(self._current.done)
        self._current = event

    def _ingest(self, item: Any) -> bool:
        """Ap dung 1 item tu queue. False neu la sentinel dung."""
        if item is _STOP:
            return False
        self._apply(item)
        return True

    def render(self) -> str:
        lines = [INITIAL_TEXT, ""]
        done = self._done_lines[-self._max_lines :]
        if len(self._done_lines) > self._max_lines:
            lines.append("…")
        lines.extend(done)
        if self._current is not None:
            lines.append(self._current.doing)
        return "\n".join(lines)

    async def _consume(self) -> None:
        alive = True
        while alive:
            item = await self._queue.get()
            alive = self._ingest(item)
            if not alive:
                break
            # Throttle: gom cac event den don dap vao mot lan edit
            wait = self._min_interval - (time.monotonic() - self._last_edit)
            if wait > 0:
                await asyncio.sleep(wait)
            while not self._queue.empty():
                alive = self._ingest(self._queue.get_nowait()) and alive
            await self._try_edit()
        # Truoc khi dung: neu con tien do chua kip hien thi thi edit not lan cuoi
        await self._try_edit()

    async def _try_edit(self) -> None:
        if self._finalized:
            return
        text = self.render()
        if text == self._last_text:
            return
        try:
            await self._status.edit_text(text)
            self._last_text = text
        except Exception:
            # Rate limit / message not modified... — tien do chi la cosmetic
            logger.debug("edit tien do that bai", exc_info=True)
        finally:
            self._last_edit = time.monotonic()
