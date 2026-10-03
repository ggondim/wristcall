"""In-memory rate limit per key (IP), sliding window."""

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, limit: int, window_s: float, now: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window_s
        self._now = now
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        t = self._now()
        if len(self._hits) > 10_000:
            self._hits = {k: q for k, q in self._hits.items() if q and q[-1] > t - self._window}
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= t - self._window:
            q.popleft()
        if len(q) >= self._limit:
            return False
        q.append(t)
        return True
