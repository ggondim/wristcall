"""The uvicorn server of a cloud-mode node (decision D13, amendment I8): on SIGTERM it stops taking calls and lets the
open ones end before uvicorn shuts down, like a shop that locks the door at closing time but serves the customers
already inside. uvicorn alone closes every WebSocket (1012) the moment the signal arrives.

Shutdown budget, worst case: 30 s for the calls (`drain_s`), 5 s for uvicorn's graceful shutdown
(`timeout_graceful_shutdown`), then the lifespan: 8 s for the work after calls (`background_grace_s`) and 20 s for
the outbox (`cloud_stop_s`), about 63 s. The container's `stop_grace_period` must be at least 75 s, or the
orchestrator kills the node mid-drain.
"""

import asyncio
import logging
import socket
from contextlib import suppress
from types import FrameType

import uvicorn

from ..admission import CallSlots
from .node import CloudNode

log = logging.getLogger("wristcall.cloud_node.draining")

DRAIN_S = 30.0
GRACEFUL_SHUTDOWN_S = 5


class DrainingServer(uvicorn.Server):
    """`handle_exit` runs as a `signal.signal` callback, outside the event loop: it only sets flags. The first signal
    asks for the drain; `on_tick` (in the loop, every 0.1 s) starts `node.drain_calls(slots, drain_s)` and lets uvicorn
    exit once it returns. A second signal forces the exit (no drain, no graceful wait)."""

    def __init__(
        self, config: uvicorn.Config, *, node: CloudNode, slots: CallSlots, drain_s: float = DRAIN_S,
        poll_s: float = 0.1,
    ) -> None:
        super().__init__(config)
        self.node = node
        self.slots = slots
        self.drain_s = drain_s
        self.poll_s = poll_s
        self.drain_task: asyncio.Task[int] | None = None
        self._exit_asked = False

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)  # records the signal (uvicorn raises it again after shutting down)
        if self._exit_asked:
            self.force_exit = True
            self.should_exit = True
            return
        self._exit_asked = True
        self.should_exit = False  # not yet: on_tick drains the calls first

    async def on_tick(self, counter: int) -> bool:
        if self._exit_asked and not self.should_exit:
            if self.drain_task is None:
                log.info("stop requested: draining open calls (up to %g s)", self.drain_s)
                self.drain_task = asyncio.create_task(
                    self.node.drain_calls(self.slots, self.drain_s, poll_s=self.poll_s)
                )
            elif self.drain_task.done():
                if not self.drain_task.cancelled() and self.drain_task.exception() is not None:
                    log.error("draining failed (%s); stopping now", type(self.drain_task.exception()).__name__)
                self.should_exit = True
        return await super().on_tick(counter)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        if self.drain_task is not None and not self.drain_task.done():  # a second signal cut the drain short
            self.drain_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.drain_task
        await super().shutdown(sockets)

