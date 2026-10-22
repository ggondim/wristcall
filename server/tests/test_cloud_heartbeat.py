"""The node's heartbeat to the Cloud (cloud mode): its body, readiness after the first accepted beat, retries."""

import asyncio
import contextlib
import logging

import pytest
from cloud_node_helpers import Sleeps, client_of, new_fake

from wristcall.admission import CallSlots
from wristcall.cloud_node.heartbeat import Heartbeat
from wristcall.cloud_node.outbox import CallRegistry
from wristcall.config import ConcurrentCallsConfig


@pytest.fixture
def fake():
    return new_fake()


@pytest.fixture
def client(fake):
    return client_of(fake)


def heartbeat(client, registry=None, slots=None, **kw) -> Heartbeat:
    return Heartbeat(
        client, registry or CallRegistry(), slots or CallSlots(ConcurrentCallsConfig()),
        ConcurrentCallsConfig(conversation=3, one_way=None), version=kw.pop("version", "0.10.0"), **kw,
    )


async def test_heartbeat_body(fake, client):
    registry = CallRegistry()
    registry.open("c_1", "g1")
    registry.open("c_2", "g2")
    slots = CallSlots(ConcurrentCallsConfig(conversation=3, one_way=10))
    slot = slots.try_acquire("one_way")
    assert slot is not None
    hb = heartbeat(client, registry, slots, boot_id="boot-1")
    assert await hb.beat() is True
    assert fake.heartbeats == [{
        "boot_id": "boot-1", "version": "0.10.0", "active_call_ids": ["c_1", "c_2"],
        "active_by_type": {"conversation": 0, "one_way": 1}, "capacity": {"conversation": 3, "one_way": None},
        "providers_ok": True,
    }]
    assert all("x-wristcall-call-grant" not in r["headers"] for r in fake.requests)


async def test_heartbeat_cuts_to_the_cloud_limits(fake, client):
    registry = CallRegistry()
    for n in range(600):
        registry.open(f"c_{n:016x}", "g")
    hb = heartbeat(client, registry, version="0.10.0+" + "x" * 40)
    assert await hb.beat() is True
    body = fake.heartbeats[-1]
    assert body["active_call_ids"] == registry.ids()[:500]
    assert len(body["version"]) == 32
    assert 0 < len(body["boot_id"]) <= 64
    assert heartbeat(client).boot_id != hb.boot_id  # a new one per start


async def test_heartbeat_refused_is_false(fake, client):
    fake.retry_after = 5
    assert await heartbeat(client).beat() is False
    fake.retry_after = None
    fake.down = True
    assert await heartbeat(client).beat() is False


async def test_heartbeat_body_and_ready(fake, client, caplog):
    ready = asyncio.Event()
    seen: list[bool] = []

    def hook(n: int) -> None:
        seen.append(ready.is_set())
        if n == 3:
            fake.down = False

    sleeps = Sleeps(hook)
    hb = heartbeat(client, every_s=15.0, sleep=sleeps)
    fake.down = True
    caplog.set_level(logging.INFO, logger="wristcall.cloud_node.heartbeat")
    task = asyncio.create_task(hb.run(ready))
    try:
        async with asyncio.timeout(5):
            await ready.wait()
            while len(sleeps.waits) < 5:
                await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert seen[:3] == [False, False, False]  # Cloud down: not ready, tried again every every_s
    assert set(sleeps.waits) == {15.0}
    assert len(fake.heartbeats) >= 2  # it goes on beating once ready
    mine = [r for r in caplog.records if r.name == "wristcall.cloud_node.heartbeat"]
    failures = [r for r in mine if r.levelno >= logging.WARNING]
    assert len(failures) == 1  # logged once per stretch of failures
