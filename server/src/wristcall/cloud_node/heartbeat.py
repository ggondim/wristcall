"""The node's heartbeat (cloud mode, decisions D8 to D10): every `every_s` seconds it tells the Cloud it is up, which
calls it holds and how many more it can take, the "we are open" a branch sends the head office. The node is ready for
calls only after the first beat the Cloud accepts (`ready`).
"""

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from ..admission import CallSlots
from ..config import ConcurrentCallsConfig
from ..storage.models import CloudUnavailable
from .client import CloudClient
from .outbox import CallRegistry

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
        # Set when the node drains before exiting: it then offers no capacity, so the Cloud routes no new call here.
        self.draining = False

    def body(self) -> dict[str, Any]:
        capacity = (
            {"conversation": 0, "one_way": 0} if self.draining
            else {"conversation": self._capacity.conversation, "one_way": self._capacity.one_way}
        )
        return {
            "boot_id": self.boot_id,
            "version": self._version,
            "active_call_ids": self._registry.ids()[:MAX_ACTIVE_IDS],
            "active_by_type": self._slots.active(),
            "capacity": capacity,
            "providers_ok": True,  # D10: the operator's provider health comes in O3f
        }

    async def beat(self) -> bool:
        """One heartbeat; True when the Cloud accepted it. Logs the first failure of a run of them, and the recovery."""
        try:
            resp = await self._client.request("POST", "/v1/node/heartbeat", json=self.body())
        except CloudUnavailable:
            why = "Wristcall Cloud unavailable"
        else:
            if 200 <= resp.status < 300:
                if self._failing:
                    log.info("heartbeat: accepted again")
                    self._failing = False
                return True
            why = f"Cloud answered {resp.status} ({resp.code or 'no code'})"
        if not self._failing:
            log.warning("heartbeat: not accepted: %s; trying again every %g s", why, self._every_s)
            self._failing = True
        return False

    async def run(self, ready: asyncio.Event) -> None:
        """Beats until cancelled; sets `ready` at the first accepted beat (and leaves it set)."""
        while True:
            if await self.beat():
                ready.set()
            await self._sleep(self._every_s)
