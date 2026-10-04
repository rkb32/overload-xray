"""A small in-memory rate limiter: at most `max_events` per `per_seconds` for each key (for example, a client address).

It lives in one process, which is enough for one small container. Behind several instances you would need a shared store.
"""
import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_events: int, per_seconds: float, max_keys: int = 10_000, clock=time.monotonic):
        self.max_events = max_events
        self.per_seconds = per_seconds
        self.max_keys = max_keys  # a bound on memory: an attacker cannot grow this without limit
        self._clock = clock
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._events) >= self.max_keys and key not in self._events:
                self._forget_idle(now)
            window = self._events.setdefault(key, deque())
            while window and now - window[0] >= self.per_seconds:
                window.popleft()
            if len(window) >= self.max_events:
                return False
            window.append(now)
            return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until `key` may try again (0 if it may now)."""
        now = self._clock()
        with self._lock:
            window = self._events.get(key)
            if not window or len(window) < self.max_events:
                return 0
            return max(1, int(self.per_seconds - (now - window[0])) + 1)

    def _forget_idle(self, now: float) -> None:
        for key in [k for k, w in self._events.items() if not w or now - w[-1] >= self.per_seconds]:
            del self._events[key]
        if len(self._events) >= self.max_keys:  # still full of active keys: drop the oldest half
            for key in list(self._events)[: self.max_keys // 2]:
                del self._events[key]
