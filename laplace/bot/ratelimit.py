"""Rate limit theo user cho bot Telegram (sliding window, in-memory).

Mot user spam tin nhan se lam nghen agent loop (moi task ton nhieu LLM call
va tool call), chan luon nguoi dung khac. Limiter nay dem so request cua tung
Telegram user id trong cua so truot `window_s` giay; vuot `max_requests` thi
tu choi va cho biet can doi bao lau.

Thread-safe (handlers chay tren event loop nhung run_task chay o thread khac);
trang thai in-memory — du cho bot 1 process, restart la reset (chap nhan duoc
cho MVP).
"""

import threading
import time
from collections import deque
from collections.abc import Callable, Hashable


class RateLimiter:
    """Sliding-window rate limiter theo key (vd: Telegram user id)."""

    def __init__(
        self,
        max_requests: int = 6,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_requests < 1:
            raise ValueError("max_requests phai >= 1")
        if window_s <= 0:
            raise ValueError("window_s phai > 0")
        self.max_requests = max_requests
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, dq: deque[float], now: float) -> None:
        while dq and now - dq[0] >= self.window_s:
            dq.popleft()

    def allow(self, key: Hashable) -> bool:
        """True neu request duoc nhan (va duoc tinh vao quota); False neu vuot."""
        now = self._clock()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            self._prune(dq, now)
            if len(dq) >= self.max_requests:
                return False
            dq.append(now)
            return True

    def retry_after(self, key: Hashable) -> float:
        """So giay can doi truoc khi request tiep theo co the duoc nhan (0 = ngay)."""
        now = self._clock()
        with self._lock:
            dq = self._hits.get(key)
            if dq is None:
                return 0.0
            self._prune(dq, now)
            if len(dq) < self.max_requests:
                return 0.0
            return max(0.0, self.window_s - (now - dq[0]))

    def reset(self, key: Hashable | None = None) -> None:
        """Xoa trang thai cua mot key (hoac tat ca) — dung trong test/van hanh."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
