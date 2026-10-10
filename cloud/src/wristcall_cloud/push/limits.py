"""In-memory rate limits with fixed windows: each key's window starts at its first request and lasts `length`
seconds. Counters live in the process (one replica) and are lost on restart, which is accepted."""

import time
from collections.abc import Callable

SWEEP_EVERY = 1000  # calls between two sweeps of the windows that are over


class _Window:
    def __init__(self, length: float, limit: int) -> None:
        self.length = length
        self.limit = limit
        self.counts: dict[str, tuple[float, int]] = {}  # key -> (start of its window, requests in it)

    def wait(self, key: str, now: float) -> float | None:
        """Seconds until `key` may go again, None when it may go now."""
        start, count = self.counts.get(key, (now, 0))
        if now - start >= self.length or count < self.limit:
            return None
        return start + self.length - now

    def take(self, key: str, now: float) -> None:
        start, count = self.counts.get(key, (now, 0))
        if now - start >= self.length:
            start, count = now, 0
        self.counts[key] = (start, count + 1)

    def sweep(self, now: float) -> None:
        self.counts = {k: v for k, v in self.counts.items() if now - v[0] < self.length}


class _Limiter:
    def __init__(self, windows: list[_Window], now: Callable[[], float]) -> None:
        self._windows = windows
        self._now = now
        self._calls = 0

    def allow(self, key: str) -> float | None:
        """None: the request may go (and is counted). A number: seconds until it may (nothing is counted)."""
        now = self._now()
        self._calls += 1
        if self._calls % SWEEP_EVERY == 0:
            for window in self._windows:
                window.sweep(now)
        waits = [w for window in self._windows if (w := window.wait(key, now)) is not None]
        if waits:
            return max(waits)
        for window in self._windows:
            window.take(key, now)
        return None

    def size(self) -> int:
        """How many keys are tracked."""
        return len(set().union(*(window.counts for window in self._windows)))


class SendLimiter(_Limiter):
    """Sends per push key (by its hash): `per_minute` and `per_day`."""

    def __init__(self, per_minute: int, per_day: int, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__([_Window(60, per_minute), _Window(86400, per_day)], now)


class RegistrationLimiter(_Limiter):
    """Anonymous registrations per client address."""

    def __init__(self, per_minute: int, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__([_Window(60, per_minute)], now)
