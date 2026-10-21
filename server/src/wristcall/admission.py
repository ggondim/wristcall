"""Admission: counts the calls open at once per kind against `limits.max_concurrent_calls`.

Synchronous on purpose: every call runs on one event loop and there is no `await` between checking a count and
adding to it, so no lock is needed."""

from collections.abc import Callable
from typing import Literal

from .agents import ONE_WAY
from .config import ConcurrentCallsConfig

CallKind = Literal["conversation", "one_way"]


def call_kind(call_type: str) -> CallKind:
    return "one_way" if call_type in ONE_WAY else "conversation"


class Slot:
    """One admitted call. `release()` gives the place back once, however many times it is called."""

    def __init__(self, kind: CallKind, free: Callable[[CallKind], None]) -> None:
        self.kind = kind
        self._free = free
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._free(self.kind)


class CallSlots:
    def __init__(self, limits: ConcurrentCallsConfig) -> None:
        self._limits: dict[CallKind, int | None] = {"conversation": limits.conversation, "one_way": limits.one_way}
        self._active: dict[CallKind, int] = {"conversation": 0, "one_way": 0}

    def try_acquire(self, kind: CallKind) -> Slot | None:
        """A slot for one more call of this kind, or None when the kind is at its limit."""
        limit = self._limits[kind]
        if limit is not None and self._active[kind] >= limit:
            return None
        self._active[kind] += 1
        return Slot(kind, self._free)

    def active(self) -> dict[CallKind, int]:
        return dict(self._active)

    def _free(self, kind: CallKind) -> None:
        self._active[kind] = max(0, self._active[kind] - 1)
