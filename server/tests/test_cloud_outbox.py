"""The node's in-memory outbox (cloud mode): FIFO, retries, overflow, drain; and the registry of open calls."""

import asyncio
import contextlib
import logging

import httpx
import pytest
import respx
from cloud_node_helpers import ACCOUNT, Sleeps, client_of, new_fake, open_call, until

from wristcall.cloud_node.outbox import CallRegistry, Outbox, OutboxItem


def entries(call_id: str, grant: str, seq: int, text: str = "c2VhbGVk") -> OutboxItem:
    body = {"entries": [{"seq": seq, "role": "user", "text": text, "sealed": True, "error": None, "at": 1.0,
                         "term_hashes": []}]}
    return OutboxItem(call_id, "entries", f"/v1/node/calls/{call_id}/entries", body, grant)


def patch(call_id: str, grant: str, **body) -> OutboxItem:
    return OutboxItem(call_id, "patch", f"/v1/node/calls/{call_id}", body or {"attempts": 1}, grant)


def finish(call_id: str, grant: str, status: str = "ended", seconds: int = 3) -> OutboxItem:
    body = {"status": status, "seconds": seconds}
    return OutboxItem(call_id, "finish", f"/v1/node/calls/{call_id}/finish", body, grant)


@contextlib.asynccontextmanager
async def running(outbox: Outbox):
    task = asyncio.create_task(outbox.run())
    await asyncio.sleep(0)  # the worker is running before the test goes on
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def fake():
    return new_fake()


@pytest.fixture
def client(fake):
    return client_of(fake)


def node_requests(fake, since: int = 0) -> list[tuple[str, str]]:
    return [(r["method"], r["path"]) for r in fake.requests[since:]]


def test_item_size_is_compact_json_bytes():
    item = OutboxItem("c_1", "patch", "/v1/node/calls/c_1", {"status": "ended", "error": "é"}, "secret-grant")
    assert item.size == len('{"status":"ended","error":"é"}'.encode())
    assert "secret-grant" not in repr(item) and "ended" not in repr(item)


def test_registry_keeps_grant_until_forgotten():
    registry = CallRegistry()
    registry.open("c_1", "g1")
    registry.open("c_2", "g2")
    assert registry.ids() == ["c_1", "c_2"]
    registry.finished("c_1")
    assert registry.ids() == ["c_2"]
    assert registry.grant("c_1") == "g1"  # the push still needs it (I2)
    registry.forget("c_1")
    assert registry.grant("c_1") is None
    assert registry.grant("c_9") is None
    registry.finished("c_9")  # unknown: nothing happens
    registry.forget("c_9")


async def test_fifo_and_retry_order(fake, client):
    call_id, grant = open_call(fake)
    registry = CallRegistry()
    registry.open(call_id, grant)
    seen: list[bool] = []

    def hook(n: int) -> None:
        seen.append(call_id in registry.ids())
        if n == 4:
            fake.down = False

    sleeps = Sleeps(hook)
    outbox = Outbox(client, registry, sleep=sleeps)
    fake.down = True
    outbox.put(entries(call_id, grant, 0))
    outbox.put(entries(call_id, grant, 1))
    outbox.put(finish(call_id, grant))
    assert len(outbox) == 3 and not outbox.idle
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert sleeps.waits == [1, 2, 4, 8]
    assert seen == [True] * 4  # the call stays in the heartbeat until its finish is accepted
    assert node_requests(fake)[-3:] == [
        ("POST", f"/v1/node/calls/{call_id}/entries"),
        ("POST", f"/v1/node/calls/{call_id}/entries"),
        ("POST", f"/v1/node/calls/{call_id}/finish"),
    ]
    assert sorted(fake.entries[call_id]) == [0, 1]
    assert fake.calls[call_id]["open"] is False and fake.calls[call_id]["status"] == "ended"
    assert registry.ids() == [] and len(outbox) == 0 and outbox.dropped == 0


async def test_retry_delay_is_capped(fake, client):
    call_id, grant = open_call(fake)

    def hook(n: int) -> None:
        if n == 6:
            fake.down = False

    sleeps = Sleeps(hook)
    outbox = Outbox(client, CallRegistry(), max_delay_s=5.0, sleep=sleeps)
    fake.down = True
    outbox.put(patch(call_id, grant))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert sleeps.waits == [1, 2, 4, 5, 5, 5]
    assert fake.calls[call_id]["attempts"] == 1


async def test_retry_after_is_respected(fake, client):
    call_id, grant = open_call(fake)

    def hook(n: int) -> None:
        fake.retry_after = None

    sleeps = Sleeps(hook)
    outbox = Outbox(client, CallRegistry(), sleep=sleeps)
    fake.retry_after = 7
    outbox.put(patch(call_id, grant))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert sleeps.waits == [7]
    assert fake.calls[call_id]["attempts"] == 1


async def test_4xx_drops_item_and_logs_without_body(fake, client, caplog):
    call_id, grant = open_call(fake)
    registry = CallRegistry()
    registry.open(call_id, grant)
    sleeps = Sleeps()
    outbox = Outbox(client, registry, sleep=sleeps)
    caplog.set_level(logging.DEBUG)
    outbox.put(entries(call_id, grant, 0, text="not-sealed-secret-words"))
    outbox.put(OutboxItem(call_id, "entries", f"/v1/node/calls/{call_id}/entries",
                          {"entries": [{"seq": 1, "role": "robot", "text": "secret words", "sealed": True, "at": 1.0}]},
                          grant))
    outbox.put(patch(call_id, grant, attempts=2))
    outbox.put(finish(call_id, grant, status="recording"))  # the Cloud refuses it: 422
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert sleeps.waits == []
    assert sorted(fake.entries[call_id]) == [0]
    assert fake.calls[call_id]["attempts"] == 2
    assert outbox.dropped == 2
    assert registry.ids() == []  # a dropped finish frees the call too
    assert "422" in caplog.text and "invalid" in caplog.text
    assert "secret words" not in caplog.text and grant not in caplog.text


async def test_any_2xx_is_accepted(fake, client):
    call_id, grant = open_call(fake)
    fake.push_targets[f"{ACCOUNT}:{fake.calls[call_id]['device_id']}"] = "wc_push_" + "k" * 43
    outbox = Outbox(client, CallRegistry(), sleep=Sleeps())
    outbox.put(OutboxItem(call_id, "push", "/v1/node/push",
                          {"event": "call.finished", "title": "Saved", "body": "Saved.", "data": {}, "call": call_id},
                          grant))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert len(fake.pushes) == 1 and outbox.dropped == 0  # 202


async def test_401_is_retried_for_15_minutes_then_dropped(fake, client, caplog):
    call_id, grant = open_call(fake)
    clock = [0.0]

    def hook(n: int) -> None:
        clock[0] += 300.0  # five minutes per wait

    sleeps = Sleeps(hook)
    outbox = Outbox(client, CallRegistry(), sleep=sleeps, clock=lambda: clock[0])
    outbox.put(patch(call_id, "grant-of-someone-else"))
    outbox.put(patch(call_id, grant, attempts=5))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert len(sleeps.waits) == 3  # 0, 300, 600 s retried; at 900 s dropped
    assert outbox.dropped == 1
    assert fake.calls[call_id]["attempts"] == 5
    assert "401" in caplog.text and "grant-of-someone-else" not in caplog.text


async def test_401_window_starts_again_after_an_answer(fake, client):
    call_id, grant = open_call(fake)
    clock = [0.0]
    sleeps = Sleeps(lambda n: clock.__setitem__(0, clock[0] + 600.0))
    outbox = Outbox(client, CallRegistry(), sleep=sleeps, clock=lambda: clock[0])
    outbox.put(patch(call_id, "bad-1"))
    async with running(outbox):
        await until(lambda: outbox.idle)
        assert len(sleeps.waits) == 2
        outbox.put(patch(call_id, grant, attempts=3))
        await until(lambda: outbox.idle)
        outbox.put(patch(call_id, "bad-2"))
        await until(lambda: outbox.idle)
    assert len(sleeps.waits) == 4  # the 401 after a success has 15 minutes of its own
    assert outbox.dropped == 2


def test_overflow_drops_oldest_entries_first(caplog):
    registry = CallRegistry()
    registry.open("c_1", "g")
    outbox = Outbox(None, registry, max_items=3)
    first, second = entries("c_1", "g", 0), entries("c_1", "g", 1)
    p, f = patch("c_1", "g"), finish("c_1", "g")
    for item in (first, second, p, f):
        outbox.put(item)
    assert outbox.pending() == [second, p, f]
    assert outbox.dropped == 1 and len(outbox) == 3
    assert "entries" in caplog.text and "c_1" in caplog.text


def test_overflow_by_bytes_drops_oldest_entries_first():
    items = [OutboxItem("c_1", kind, "/p", {"n": 1}, "g") for kind in ("entries", "entries", "patch", "finish")]
    outbox = Outbox(None, CallRegistry(), max_bytes=3 * items[0].size)
    for item in items:
        outbox.put(item)
    assert outbox.pending() == items[1:]
    assert outbox.dropped == 1


def test_overflow_without_entries_drops_the_oldest_item():
    registry = CallRegistry()
    registry.open("c_1", "g")
    outbox = Outbox(None, registry, max_items=2)
    f, p1, p2 = finish("c_1", "g"), patch("c_1", "g"), patch("c_1", "g", attempts=2)
    for item in (f, p1, p2):
        outbox.put(item)
    assert outbox.pending() == [p1, p2]
    assert registry.ids() == []  # its finish was dropped


async def test_overflow_spares_the_item_being_sent(fake, client):
    call_id, grant = open_call(fake)
    gate = asyncio.Event()

    async def wait(seconds: float) -> None:
        await gate.wait()

    outbox = Outbox(client, CallRegistry(), max_items=1, sleep=wait)
    fake.down = True
    first = entries(call_id, grant, 0)
    outbox.put(first)
    async with running(outbox):
        await until(lambda: len(fake.requests) >= 1)
        second = entries(call_id, grant, 1)
        outbox.put(second)  # the head is in flight: the new one goes
        assert outbox.pending() == [first] and outbox.dropped == 1
        fake.down = False
        gate.set()
        await until(lambda: outbox.idle)
    assert sorted(fake.entries[call_id]) == [0]


async def test_drain_returns_leftovers(fake, client, caplog):
    call_id, grant = open_call(fake)
    registry = CallRegistry()
    registry.open(call_id, grant)
    outbox = Outbox(client, registry, sleep=Sleeps())
    fake.down = True
    outbox.put(entries(call_id, grant, 0))
    outbox.put(finish(call_id, grant))
    assert await outbox.drain(0.05) == 2
    assert len(outbox) == 0 and outbox.idle
    assert "2" in caplog.text and "lost" in caplog.text


async def test_drain_sends_what_it_can(fake, client):
    call_id, grant = open_call(fake)
    outbox = Outbox(client, CallRegistry(), sleep=Sleeps())
    outbox.put(entries(call_id, grant, 0))
    outbox.put(finish(call_id, grant))
    assert await outbox.drain(5.0) == 0  # no worker running: the drain sends them itself
    assert fake.calls[call_id]["open"] is False
    async with running(outbox):
        outbox.put(patch(call_id, grant, attempts=4))
        assert await outbox.drain(5.0) == 0  # with the worker running, it waits for it
    assert fake.calls[call_id]["attempts"] == 4


async def test_drain_of_an_empty_outbox_is_immediate(client):
    outbox = Outbox(client, CallRegistry())
    assert outbox.idle
    assert await outbox.drain(0.0) == 0


async def test_push_answered_401_is_dropped_at_once(fake, client, caplog):
    call_id, grant = open_call(fake)
    sleeps = Sleeps()
    outbox = Outbox(client, CallRegistry(), sleep=sleeps)
    outbox.put(OutboxItem(call_id, "push", "/v1/node/push",
                          {"event": "call.finished", "title": "Saved", "body": "Saved.", "data": {}, "call": call_id},
                          "expired-grant"))
    outbox.put(patch(call_id, grant, attempts=6))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert sleeps.waits == []  # best effort: it does not hold the calls behind it
    assert outbox.dropped == 1 and fake.calls[call_id]["attempts"] == 6
    assert "401" in caplog.text and "expired-grant" not in caplog.text


@pytest.mark.parametrize(("header", "wait"), [(0, 1.0), (-5, 1.0), ("nan", 1.0), ("inf", 1.0), ("soon", 1.0),
                                               (7, 7.0), (10_000, 600.0)])
async def test_bad_retry_after_waits_the_backoff(fake, client, header, wait):
    call_id, grant = open_call(fake)

    def hook(n: int) -> None:
        fake.retry_after = None

    sleeps = Sleeps(hook)
    outbox = Outbox(client, CallRegistry(), sleep=sleeps)
    fake.retry_after = header
    outbox.put(patch(call_id, grant))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert sleeps.waits == [wait]


async def test_unexpected_error_is_retried_and_the_worker_lives(fake, client, caplog):
    call_id, grant = open_call(fake)

    class Flaky:
        calls = 0

        async def request(self, *args, **kwargs):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("body with secret words")
            return await client.request(*args, **kwargs)

    sleeps = Sleeps()
    outbox = Outbox(Flaky(), CallRegistry(), sleep=sleeps)
    outbox.put(patch(call_id, grant, attempts=8))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert sleeps.waits == [1]
    assert fake.calls[call_id]["attempts"] == 8
    assert "RuntimeError" in caplog.text and "secret words" not in caplog.text


async def test_one_worker_at_a_time(client):
    outbox = Outbox(client, CallRegistry())
    outbox.start()
    await asyncio.sleep(0)
    assert outbox.running
    with pytest.raises(RuntimeError):
        await outbox.run()
    outbox.start()  # already running: nothing happens
    assert await outbox.drain(0.0) == 0
    assert not outbox.running


async def test_drain_stops_the_worker(fake, client):
    call_id, grant = open_call(fake)
    outbox = Outbox(client, CallRegistry(), sleep=Sleeps())
    outbox.start()
    fake.down = True
    outbox.put(patch(call_id, grant))
    await until(lambda: len(fake.requests) >= 2)
    assert await outbox.drain(0.01) == 1
    assert not outbox.running and outbox.idle
    seen = len(fake.requests)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(fake.requests) == seen  # the item counted as lost is no longer tried


async def test_drain_stops_an_outside_worker(fake, client):
    call_id, grant = open_call(fake)
    outbox = Outbox(client, CallRegistry(), sleep=Sleeps())
    task = asyncio.create_task(outbox.run())
    await asyncio.sleep(0)
    outbox.put(patch(call_id, grant, attempts=9))
    assert await outbox.drain(5.0) == 0
    assert task.done() and not outbox.running
    assert fake.calls[call_id]["attempts"] == 9


@respx.mock
async def test_a_2xx_with_a_body_that_is_not_json_is_accepted():
    """Any 2xx accepts the item, whatever the body (a proxy's HTML page, say): the status is what counts."""
    from wristcall.cloud_node.client import CloudClient

    base = "https://cloud.test"
    route = respx.post(f"{base}/v1/node/calls/c_1/finish").mock(
        return_value=httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})
    )
    registry = CallRegistry()
    registry.open("c_1", "g1")
    sleeps = Sleeps()
    outbox = Outbox(CloudClient(base, "secret", httpx.AsyncClient()), registry, sleep=sleeps)
    outbox.put(finish("c_1", "g1"))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert route.call_count == 1 and sleeps.waits == []
    assert outbox.dropped == 0 and registry.ids() == []


async def test_an_item_that_always_fails_unexpectedly_is_dropped_after_five_tries(fake, client, caplog):
    call_id, grant = open_call(fake)

    class Broken:
        calls = 0

        async def request(self, method, path, **kwargs):
            if path.endswith("/entries"):
                Broken.calls += 1
                raise RuntimeError("body with secret words")
            return await client.request(method, path, **kwargs)

    registry = CallRegistry()
    registry.open(call_id, grant)
    sleeps = Sleeps()
    outbox = Outbox(Broken(), registry, sleep=sleeps)
    outbox.put(entries(call_id, grant, 1))
    outbox.put(finish(call_id, grant))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert Broken.calls == 5 and sleeps.waits == [1, 2, 4, 8]
    assert outbox.dropped == 1
    assert registry.ids() == []  # the finish behind it went through
    assert fake.calls[call_id]["status"] == "ended"
    assert "RuntimeError" in caplog.text and "secret words" not in caplog.text
    assert "repeated errors while sending" in caplog.text and "refused by the Cloud" not in caplog.text


@respx.mock
async def test_a_non_json_4xx_is_retried_not_dropped():
    """A proxy's HTML 404 during a deploy is not the Cloud refusing the item: it waits, then goes out."""
    from wristcall.cloud_node.client import CloudClient

    base = "https://cloud.test"
    route = respx.post(f"{base}/v1/node/calls/c_1/finish").mock(side_effect=[
        httpx.Response(404, text="<html>not found</html>", headers={"content-type": "text/html"}),
        httpx.Response(404, text="<html>not found</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, json={"ok": True}),
    ])
    registry = CallRegistry()
    registry.open("c_1", "g1")
    sleeps = Sleeps()
    outbox = Outbox(CloudClient(base, "secret", httpx.AsyncClient()), registry, sleep=sleeps)
    outbox.put(finish("c_1", "g1"))
    async with running(outbox):
        await until(lambda: outbox.idle)
    assert route.call_count == 3 and sleeps.waits == [1, 2]
    assert outbox.dropped == 0 and registry.ids() == []
