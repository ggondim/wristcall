"""The node's in-memory outbox (cloud mode, decision D7) and the registry of its open calls (D8).

What a call writes to the Cloud (history entries, record updates, its finish, the push that tells the watch how it
ended) goes through one FIFO queue with one worker, so a call's entries reach the Cloud before its finish, and a Cloud
that is down for a while costs nothing but delay. Like a till drawer where the receipts wait while the head office is
offline: they leave in the order they came in, and when the drawer is full the oldest history lines go first.

- A transport error, a 5xx (both `CloudUnavailable`) or a 429 hold the head of the queue and try it again after 1, 2,
  4... seconds up to `max_delay_s` (a 429 waits its `Retry-After`).
- A 401 is tried again for up to 15 minutes (the window for rotating the node secret), then dropped.
- Any other 4xx drops the item (logged with the status and the Cloud's error code); any 2xx accepts it.
- Over `max_items` or `max_bytes`, the oldest history entries are dropped first, then the oldest item of any kind,
  never the item being sent. `dropped` counts them all.

No log carries a grant, the node secret or a body (the entries' text is sealed, but even so).
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from ..storage.models import CloudUnavailable
from .client import CloudClient

log = logging.getLogger("wristcall.cloud_node.outbox")

Kind = Literal["entries", "patch", "finish", "push"]
METHODS: dict[str, str] = {"entries": "POST", "patch": "PATCH", "finish": "POST", "push": "POST"}
UNAUTHORIZED_WINDOW_S = 15 * 60
MAX_RETRY_AFTER_S = 600.0


@dataclass(frozen=True, eq=False)
class OutboxItem:
    """One request the node owes the Cloud. `size`: bytes of its body as compact JSON (what the cap counts)."""

    call_id: str
    kind: Kind
    path: str
    body: dict = field(repr=False)
    grant: str = field(repr=False)
    size: int = field(init=False)

    def __post_init__(self) -> None:
        compact = json.dumps(self.body, separators=(",", ":"), ensure_ascii=False).encode()
        object.__setattr__(self, "size", len(compact))


class CallRegistry:
    """The calls open on this node and their grants.

    `ids()` (the heartbeat's `active_call_ids`) holds a call from `open` until its finish is accepted or dropped
    (`finished`). The grant stays longer, until the call's push is queued (the outbox forgets it then) or `forget`
    is called (a conversation, which has no push): the push may be sent after the finish (I2).
    """

    def __init__(self) -> None:
        self._open: dict[str, None] = {}  # insertion-ordered set
        self._grants: dict[str, str] = {}

    def open(self, call_id: str, grant: str) -> None:
        self._open[call_id] = None
        self._grants[call_id] = grant

    def grant(self, call_id: str) -> str | None:
        return self._grants.get(call_id)

    def finished(self, call_id: str) -> None:
        self._open.pop(call_id, None)

    def forget(self, call_id: str) -> None:
        self._grants.pop(call_id, None)

    def ids(self) -> list[str]:
        return list(self._open)


class Outbox:
    def __init__(
        self,
        client: CloudClient,
        registry: CallRegistry,
        *,
        max_items: int = 2000,
        max_bytes: int = 20_000_000,
        max_delay_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._registry = registry
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._max_delay = max_delay_s
        self._sleep = sleep
        self._clock = clock
        self._items: deque[OutboxItem] = deque()
        self._bytes = 0
        self._sending: OutboxItem | None = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._workers = 0
        self._unauthorized_since: float | None = None  # first of an unbroken run of 401 answers
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def idle(self) -> bool:
        """Nothing queued and nothing being sent."""
        return not self._items and self._sending is None

    def pending(self) -> list[OutboxItem]:
        """The queued items, oldest first (a copy)."""
        return list(self._items)

    def put(self, item: OutboxItem) -> None:
        """Queues the item; never blocks. Over the cap, drops by D7. Queuing a call's push forgets its grant."""
        self._items.append(item)
        self._bytes += item.size
        self._idle.clear()
        if item.kind == "push":
            self._registry.forget(item.call_id)
        while len(self._items) > self._max_items or self._bytes > self._max_bytes:
            victim = self._victim()
            if victim is None:
                break
            self._remove(victim)
            self._drop(victim, "the outbox is full")
        self._wake.set()

    def _victim(self) -> OutboxItem | None:
        spare = [i for i in self._items if i is not self._sending]
        return next((i for i in spare if i.kind == "entries"), spare[0] if spare else None)

    def _remove(self, item: OutboxItem) -> None:
        self._items.remove(item)
        self._bytes -= item.size

    def _drop(self, item: OutboxItem, why: str) -> None:
        self.dropped += 1
        log.warning("outbox: dropped %s of call %s: %s", item.kind, item.call_id, why)
        self._settled(item)

    def _settled(self, item: OutboxItem) -> None:
        """The Cloud took the item, or it will never get it."""
        if item.kind == "finish":
            self._registry.finished(item.call_id)

    async def run(self) -> None:
        """The worker: sends the head of the queue until cancelled. Run one at a time (a task of the node)."""
        self._workers += 1
        try:
            while True:
                if not self._items:
                    self._wake.clear()
                    self._idle.set()
                    await self._wake.wait()
                    continue
                item = self._sending = self._items[0]
                try:
                    accepted = await self._send(item)
                finally:
                    self._sending = None
                if self._items and self._items[0] is item:
                    self._remove(item)
                    if accepted:
                        self._settled(item)
                    else:
                        self._drop(item, "refused by the Cloud")
                if not self._items:
                    self._idle.set()
        finally:
            self._workers -= 1

    async def _send(self, item: OutboxItem) -> bool:
        """Tries the item until the Cloud accepts it (True) or refuses it for good (False)."""
        delay, tries = 1.0, 0
        while True:
            tries += 1
            wait = min(delay, self._max_delay)
            try:
                resp = await self._client.request(METHODS[item.kind], item.path, grant=item.grant, json=item.body)
            except CloudUnavailable:
                why = "Wristcall Cloud unavailable"
            else:
                if resp.status != 401:
                    self._unauthorized_since = None
                if 200 <= resp.status < 300:
                    if tries > 1:
                        log.info("outbox: %s of call %s sent after %d tries", item.kind, item.call_id, tries)
                    return True
                if resp.status == 429:
                    why = "rate limited (429)"
                    wait = _retry_after(resp.headers.get("retry-after"), delay)
                elif resp.status == 401:
                    now = self._clock()
                    if self._unauthorized_since is None:
                        self._unauthorized_since = now
                    if now - self._unauthorized_since >= UNAUTHORIZED_WINDOW_S:
                        log.error("outbox: %s of call %s: Cloud answered 401 for 15 minutes; dropping it",
                                  item.kind, item.call_id)
                        return False
                    why = "Cloud answered 401"
                else:
                    log.warning("outbox: %s of call %s: Cloud answered %d (%s)",
                                item.kind, item.call_id, resp.status, resp.code or "no code")
                    return False
            if tries == 1:
                log.warning("outbox: %s of call %s: %s; trying again", item.kind, item.call_id, why)
            await self._sleep(wait)
            delay = min(delay * 2, self._max_delay)

    async def drain(self, timeout_s: float) -> int:
        """Shutdown: gives the queue up to `timeout_s` to empty (with the running worker, or one of its own), then
        empties it and returns how many items were left (logged as lost)."""
        own = asyncio.create_task(self.run()) if self._workers == 0 and self._items else None
        try:
            if self._items or self._sending is not None:
                try:
                    await asyncio.wait_for(self._idle.wait(), timeout_s)
                except TimeoutError:
                    pass
        finally:
            if own is not None:
                own.cancel()
                await asyncio.gather(own, return_exceptions=True)
        left = list(self._items)
        if left:
            kinds: dict[str, int] = {}
            for item in left:
                kinds[item.kind] = kinds.get(item.kind, 0) + 1
                self._settled(item)
            log.error("outbox: %d items lost at shutdown (%s)", len(left),
                      ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items())))
            self._items.clear()
            self._bytes = 0
        self._idle.set()
        return len(left)


def _retry_after(value: str | None, fallback: float) -> float:
    """Seconds of a `Retry-After` header (delta-seconds form); the backoff when missing or not a number."""
    try:
        seconds = float(value) if value is not None else fallback
    except ValueError:
        return fallback
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)
