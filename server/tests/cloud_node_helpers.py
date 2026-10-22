"""Shared pieces of the cloud-node tests (outbox, heartbeat, call log): a recording sleep and a FakeCloud setup."""

import asyncio
from collections.abc import Callable

from fake_cloud import CLOUD_URL, FULL_PLAN, FakeCloud

from wristcall.cloud_node.client import CloudClient

NODE_URL = "https://node.test"
SECRET = "wc_node_" + "s" * 43
ACCOUNT, DEVICE = "acc-a", "dev-a"


class Sleeps:
    """Stands for `asyncio.sleep`: records each wait, runs `hook(n)` on the n-th, never waits in real time."""

    def __init__(self, hook: Callable[[int], None] | None = None) -> None:
        self.waits: list[float] = []
        self.hook = hook

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self.hook is not None:
            self.hook(len(self.waits))
        await asyncio.sleep(0)


def new_fake() -> FakeCloud:
    fake = FakeCloud(NODE_URL, SECRET)
    fake.plans[ACCOUNT] = FULL_PLAN
    return fake


def client_of(fake: FakeCloud) -> CloudClient:
    return CloudClient(CLOUD_URL, SECRET, fake.http())


def open_call(fake: FakeCloud, call_type: str = "one-shot") -> tuple[str, str]:
    """(call id, grant) of a call open on the fake Cloud, as `POST /v1/node/calls` leaves it."""
    call_id = fake.seed_call(ACCOUNT, call_type=call_type, open=True, device_id=DEVICE)
    return call_id, fake.issue_grant(call_id)


async def until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0)
