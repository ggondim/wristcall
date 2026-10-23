"""The node's heartbeat (cloud mode, decisions D8 to D10): every `every_s` seconds it tells the Cloud it is up, which
calls it holds and how many more it can take, the "we are open" a branch sends the head office. The node is ready for
calls only after the first beat the Cloud accepts (`ready`).

Each beat also carries the node's counters (`stats`, see `stats.py`) of the interval since the last accepted beat: they
are taken off only when the Cloud accepts the beat, so a run of refused beats loses nothing, and the time the Cloud was
not reached (from the first refused beat to the next accepted one) is counted too.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..admission import CallSlots
from ..config import ConcurrentCallsConfig
from ..storage.models import CloudUnavailable
from .client import CloudClient
from .outbox import CallRegistry
from .stats import NodeStats

log = logging.getLogger("wristcall.cloud_node.heartbeat")

# The Cloud's limits on a heartbeat body.
MAX_ACTIVE_IDS = 500
MAX_BOOT_ID = 64
MAX_VERSION = 32


class Heartbeat:
    def __init__(
        self,
        client: CloudClient,
        registry: CallRegistry,
        slots: CallSlots,
        capacity: ConcurrentCallsConfig,
        *,
        version: str,
        boot_id: str | None = None,
        every_s: float = 15.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stats: NodeStats | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._registry = registry
        self._slots = slots
        self._capacity = capacity
        self._version = version[:MAX_VERSION]
        # Names this run of the node: the Cloud fails a `processing` call left by an older boot.
        self.boot_id = (boot_id or secrets.token_hex(16))[:MAX_BOOT_ID]
        self._every_s = every_s
        self._sleep = sleep
        self._failing = False
        self.stats = stats or NodeStats()
        self._clock = clock
        self._unreachable_since: float | None = None  # the start of the current run of refused beats
        self._carrying = False  # a beat with `stats` is on its way: another one at once (draining) goes without
        self._stats_failing = False  # the counters raised on the last beat: logged once per run of failures
        # Set when the node drains before exiting: it then offers no capacity, so the Cloud routes no new call here.
        self.draining = False

    def body(self) -> dict[str, Any]:
        capacity = (
            {"conversation": 0, "one_way": 0} if self.draining
            else {"conversation": self._capacity.conversation, "one_way": self._capacity.one_way}
        )
        body: dict[str, Any] = {
            "boot_id": self.boot_id,
            "version": self._version,
            "active_call_ids": self._registry.ids()[:MAX_ACTIVE_IDS],
            "active_by_type": self._slots.active(),
            "capacity": capacity,
            "providers_ok": True,  # D10: the operator's provider health comes in O3f
        }
        # A metrics bug must never stop a heartbeat (and with it readiness): the beat then goes without `stats`.
        try:
            body["stats"] = self._stats_now()
        except Exception as e:  # noqa: BLE001
            if not self._stats_failing:
                log.warning("heartbeat: the node counters failed (%s); beating without them", type(e).__name__)
                self._stats_failing = True
        else:
            self._stats_failing = False
        return body

    def _stats_now(self) -> dict[str, Any]:
        self.stats.active(self._slots.active())
        return self.stats.snapshot()

    async def beat(self) -> bool:
        """One heartbeat; True when the Cloud accepted it. Logs the first failure of a run of them, and the recovery.
        The counters sent are taken off only when the Cloud accepts the beat."""
        carrying = not self._carrying
        if carrying:
            self._carrying = True
        try:
            return await self._beat(carrying)
        finally:
            if carrying:
                self._carrying = False

    async def _beat(self, carrying: bool) -> bool:
        now = self._clock()
        if carrying and self._unreachable_since is not None:
            try:
                self.stats.cloud_unreachable(now - self._unreachable_since)
            except Exception as e:  # noqa: BLE001 - a metrics bug must not skip the beat
                if not self._stats_failing:
                    log.warning("heartbeat: the node counters failed (%s); beating without them", type(e).__name__)
                    self._stats_failing = True
            self._unreachable_since = now
        body = self.body()
        if not carrying:
            body.pop("stats", None)
        try:
            resp = await self._client.request("POST", "/v1/node/heartbeat", json=body)
        except CloudUnavailable:
            why = "Wristcall Cloud unavailable"
        else:
            if 200 <= resp.status < 300:
                if carrying:
                    self._unreachable_since = None
                    if "stats" in body:
                        try:
                            self.stats.active(self._slots.active())
                            self.stats.commit(body["stats"])
                        except Exception as e:  # noqa: BLE001 - the beat was accepted; only the counters are in doubt
                            log.warning("heartbeat: the node counters could not be taken off (%s)", type(e).__name__)
                if self._failing:
                    log.info("heartbeat: accepted again")
                    self._failing = False
                return True
            why = f"Cloud answered {resp.status} ({resp.code or 'no code'})"
        if carrying and self._unreachable_since is None:
            self._unreachable_since = now
        if not self._failing:
            log.warning("heartbeat: not accepted: %s; trying again every %g s", why, self._every_s)
            self._failing = True
        return False

    async def run(self, ready: asyncio.Event) -> None:
        """Beats until cancelled; sets `ready` at the first accepted beat (and leaves it set)."""
        while True:
            try:
                if await self.beat():
                    ready.set()
            except Exception as e:  # noqa: BLE001 - a dead heartbeat would leave the node taking calls the Cloud thinks are lost
                log.error("heartbeat: a beat failed unexpectedly (%s); going on", type(e).__name__)
            await self._sleep(self._every_s)
