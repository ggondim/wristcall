"""The node's heartbeat to the Cloud (cloud mode): its body, readiness after the first accepted beat, retries."""

import asyncio
import contextlib
import logging

import pytest
from cloud_app_fixtures import cloud_app, cloud_config, wait_ready
from cloud_app_fixtures import new_fake as new_app_fake
from cloud_node_helpers import Sleeps, client_of, new_fake
from fake_cloud import FakeCloud
from fastapi.testclient import TestClient

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
        "providers_ok": True, "stats": {
            "calls": {"conversation": 0, "one_way": 0}, "busy": {"conversation": 0, "one_way": 0}, "not_ready": 0,
            "peak": {"conversation": 0, "one_way": 1}, "first_audio_ms": {}, "stt_ms": {}, "tts_ms": {},
            "provider_failures": {}, "cloud_unreachable_s": 0, "outbox_dropped": 0,
        },
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


async def test_heartbeat_survives_an_unexpected_error(fake, client, caplog):
    """A beat that raises something unforeseen is logged by type and the loop goes on: a dead heartbeat would leave
    the node taking calls the Cloud believes lost."""
    sleeps = Sleeps()
    hb = heartbeat(client, every_s=15.0, sleep=sleeps)
    real_beat = hb.beat
    calls = 0

    async def flaky() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret-token-text")
        return await real_beat()

    hb.beat = flaky  # type: ignore[method-assign]
    ready = asyncio.Event()
    caplog.set_level(logging.INFO, logger="wristcall.cloud_node.heartbeat")
    task = asyncio.create_task(hb.run(ready))
    try:
        async with asyncio.timeout(5):
            await ready.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert calls >= 2 and len(fake.heartbeats) >= 1  # the next beat was sent
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and "secret-token-text" not in text


# --- the node's counters (server 0.11.0) ----------------------------------------------------------------------------

STATS_KEYS = {
    "calls", "busy", "not_ready", "peak", "first_audio_ms", "stt_ms", "tts_ms", "provider_failures",
    "cloud_unreachable_s", "outbox_dropped",
}


async def test_body_has_stats(fake, client):
    hb = heartbeat(client)
    assert set(hb.body()["stats"]) == STATS_KEYS
    assert await hb.beat() is True
    assert set(fake.heartbeats[-1]["stats"]) == STATS_KEYS
    assert fake.node_stats == [fake.heartbeats[-1]["stats"]]


async def test_beat_while_another_carries_stats_goes_without(fake, client):
    """The drain's beat may run next to a regular one: only one of them carries (and takes off) the counters."""
    hb = heartbeat(client)
    hb.stats.busy("one_way")
    await asyncio.gather(hb.beat(), hb.beat())
    with_stats = [b for b in fake.heartbeats if "stats" in b]
    assert len(fake.heartbeats) == 2 and len(with_stats) == 1
    assert with_stats[0]["stats"]["busy"]["one_way"] == 1
    assert hb.stats.snapshot()["busy"]["one_way"] == 0


START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}
VOICE_NOTES = {
    "id": "ag_0123456789ab", "slug": "voice-notes", "display_name": "Voice notes", "icon": "mic", "color": None,
    "call_type": "one-shot", "position": 0, "created_at": 1.0, "updated_at": 1.0, "account": "acc-1",
    "spec": {"language": "en", "stt": {"provider": "stt"}, "action": None, "retention_days": None},
}


def _refused(c: TestClient, fake: FakeCloud) -> dict:
    with c.websocket_connect("/v1/call", headers=_bearer(fake)) as ws:
        ws.send_json(START)
        return ws.receive_json()


def _bearer(fake: FakeCloud) -> dict[str, str]:
    return {"Authorization": f"Bearer {fake.user_token('acc-1', 'dev-watch', 'watchos')}"}


def test_busy_refusal_counts():
    """A call refused for a full slot counts in `busy` of its kind; one refused because the node is not ready (or is
    draining) counts in `not_ready`, not in `busy`."""
    fake = new_app_fake()
    fake.plans["acc-1"] = {"plan": "full", "source": "beta", "expires_at": None}
    fake.agents[VOICE_NOTES["id"]] = dict(VOICE_NOTES)
    config = cloud_config(providers={"stt": {"type": "fake_stt", "text": "hi"}})
    with TestClient(cloud_app(fake, config)) as c:
        wait_ready(c)
        fake.heartbeat_answers = [503] * 10_000  # from here on nothing is taken off: every count stays on the node
        node = c.app.state.cloud_node
        held = [c.app.state.call_slots.try_acquire("one_way") for _ in range(2)]  # cloud default: 2 one-way calls
        assert None not in held
        msg = _refused(c, fake)
        assert (msg["type"], msg["code"]) == ("error", "busy")
        for slot in held:
            slot.release()
        node.draining = True
        msg = _refused(c, fake)
        assert (msg["type"], msg["code"]) == ("error", "busy")
        snap = node.stats.snapshot()
    assert snap["busy"] == {"conversation": 0, "one_way": 1}
    assert snap["not_ready"] == 1
    assert snap["calls"] == {"conversation": 0, "one_way": 0}  # neither call was opened
