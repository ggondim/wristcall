"""CloudCallLog and CloudPushNotifier (cloud mode): a call's history and its push go to the Cloud through the outbox,
with the call grant; the text sealed and bound to the account."""

import asyncio
import contextlib
import logging
import re
from dataclasses import replace

import pytest
from cloud_node_helpers import ACCOUNT, DEVICE, Sleeps, client_of, new_fake, open_call, until

from wristcall.cloud_node.calls import CloudCallLog, CloudPushNotifier
from wristcall.cloud_node.outbox import CallRegistry, Outbox
from wristcall.history import History, aad_scoped
from wristcall.history_codec import HistoryCodec
from wristcall.push import _call_text, call_text
from wristcall.storage import CallRecord

KEY = bytes(range(32))
PUSH_KEY = "wc_push_" + "k" * 43
HASH = re.compile(r"^x[0-9a-f]{16}$")


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def fake():
    return new_fake()


@pytest.fixture
def registry():
    return CallRegistry()


@pytest.fixture
def outbox(fake, registry):
    return Outbox(client_of(fake), registry, sleep=Sleeps())


@contextlib.asynccontextmanager
async def running(outbox: Outbox):
    task = asyncio.create_task(outbox.run())
    await asyncio.sleep(0)  # the worker is running before the test goes on
    try:
        yield
        await until(lambda: outbox.idle)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def record_of(fake, call_id: str, **over) -> CallRecord:
    call = fake.calls[call_id]
    fields = dict(
        id=call_id, user_id=ACCOUNT, agent_id=call["agent_id"], device_id=DEVICE, call_type=call["call_type"],
        status="recording", created_at=call["created_at"], updated_at=call["created_at"], agent_slug="note",
        agent_name="Note", delivery=call["delivery"],
    )
    return CallRecord(**{**fields, **over})


def call_log(fake, outbox, registry, call_type="one-shot", **kw) -> CloudCallLog:
    call_id, grant = open_call(fake, call_type)
    registry.open(call_id, grant)
    return CloudCallLog(record_of(fake, call_id), grant, HistoryCodec(KEY), outbox, registry, **kw)


def queued(outbox: Outbox) -> list[tuple[str, dict]]:
    return [(i.kind, i.body) for i in outbox.pending()]


def test_call_text_is_public():
    assert _call_text is call_text


async def test_call_log_entries_are_sealed_and_scoped(fake, outbox, registry, caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock(50.0)
    log = call_log(fake, outbox, registry, call_type="conversation", now=clock)
    await log.add("user", "Comprar leite amanhã")
    clock.t = 51.0
    await log.add("agent", None, "tts_failed")
    assert log.count == 2
    async with running(outbox):
        pass
    stored = fake.entries[log.record.id]
    first, second = stored[0], stored[1]
    assert first["sealed"] is True and first["role"] == "user" and first["at"] == 50.0
    codec = HistoryCodec(KEY)
    assert codec.open(first["text"], True, aad_scoped(ACCOUNT, log.record.id, 0)) == "Comprar leite amanhã"
    assert first["term_hashes"] == codec.for_account(ACCOUNT).index_terms("Comprar leite amanhã")
    assert first["term_hashes"] != codec.index_terms("Comprar leite amanhã")  # the account's own index key
    assert all(HASH.fullmatch(h) for h in first["term_hashes"])
    assert second == {"seq": 1, "role": "agent", "text": None, "sealed": False, "error": "tts_failed", "at": 51.0,
                      "term_hashes": []}
    assert "leite" not in caplog.text and "Comprar" not in caplog.text


async def test_call_log_cuts_term_hashes(fake, outbox, registry):
    log = call_log(fake, outbox, registry)
    await log.add("user", " ".join(f"w{n}" for n in range(2500)))
    [(kind, body)] = queued(outbox)
    assert kind == "entries" and len(body["entries"][0]["term_hashes"]) == 2000
    async with running(outbox):
        pass
    assert len(fake.entries[log.record.id][0]["term_hashes"]) == 2000


def test_call_log_needs_a_history_key(fake, outbox, registry):
    call_id, grant = open_call(fake)
    with pytest.raises(ValueError):
        CloudCallLog(record_of(fake, call_id), grant, HistoryCodec(), outbox, registry)


async def test_call_log_add_never_raises(fake, outbox, registry, caplog):
    log = call_log(fake, outbox, registry)
    log._codec = None  # anything going wrong while sealing
    await log.add("user", "hello")
    assert log.count == 1 and len(outbox) == 0
    assert "could not record utterance 0" in caplog.text


async def test_call_log_finish_seconds(fake, outbox, registry):
    clock = Clock(170.0)
    log = call_log(fake, outbox, registry, call_type="conversation", now=clock)
    assert log.seconds == 0
    log.mark_ready(100.0)
    saved = await log.save(status="ended", ended_at=161.2, finished=True)
    assert saved is log.record and saved.status == "ended" and saved.finished_at == 170.0 and saved.updated_at == 170.0
    assert log.seconds == 62
    assert queued(outbox) == [
        ("patch", {"ended_at": 161.2, "finished_at": 170.0}),
        ("finish", {"status": "ended", "seconds": 62}),
    ]
    async with running(outbox):
        pass
    call = fake.calls[log.record.id]
    assert call["open"] is False and call["seconds"] == 62 and call["status"] == "ended"
    assert registry.ids() == []


async def test_call_log_finish_without_ready_is_zero_seconds(fake, outbox, registry):
    log = call_log(fake, outbox, registry, call_type="conversation", now=Clock())
    await log.save(status="empty", finished=True)
    assert queued(outbox) == [("patch", {"finished_at": 1000.0}), ("finish", {"status": "empty", "seconds": 0})]


async def test_one_way_finishes_at_hang_up_and_patches_the_outcome(fake, outbox, registry):
    clock = Clock(200.0)
    log = call_log(fake, outbox, registry, now=clock)
    log.mark_ready(190.0)
    await log.save(status="processing", ended_at=199.5)  # OneWayCall.finish, before the STT
    assert queued(outbox) == [("patch", {"ended_at": 199.5}), ("finish", {"status": "processing", "seconds": 10})]
    async with running(outbox):
        pass
    assert registry.ids() == []  # the account's call slot is free at hang-up (D21)
    assert fake.calls[log.record.id]["open"] is False
    clock.t = 205.0
    await log.save(status="failed", error="stt_failed", finished=True)
    assert queued(outbox) == [("patch", {"status": "failed", "error": "stt_failed", "finished_at": 205.0})]
    async with running(outbox):
        pass
    call = fake.calls[log.record.id]
    assert call["status"] == "failed" and call["error"] == "stt_failed" and call["finished_at"] == 205.0


async def test_finish_carries_the_error(fake, outbox, registry):
    log = call_log(fake, outbox, registry, now=Clock())
    await log.save(status="failed", error="interrupted", finished=True)
    assert queued(outbox)[-1] == ("finish", {"status": "failed", "seconds": 0, "error": "interrupted"})
    assert queued(outbox)[0] == ("patch", {"finished_at": 1000.0})


async def test_saves_before_the_finish_are_patches(fake, outbox, registry):
    log = call_log(fake, outbox, registry, now=Clock())
    await log.save(delivery="history", agent_name="Ignored")
    await log.save(agent_slug="nothing-the-cloud-takes")
    assert queued(outbox) == [("patch", {"delivery": "history"})]
    assert log.record.agent_slug == "nothing-the-cloud-takes"


async def test_redelivery_log_never_finishes(fake, outbox, registry):
    log = call_log(fake, outbox, registry, now=Clock(), report_finish=False)
    await log.save(status="processing", error=None, finished_at=None)
    await log.save(status="delivered", attempts=1, last_http_status=200, finished=True)
    assert queued(outbox) == [
        ("patch", {"status": "processing", "error": None, "finished_at": None}),
        ("patch", {"status": "delivered", "attempts": 1, "last_http_status": 200, "finished_at": 1000.0}),
    ]


async def test_items_carry_the_grant(fake, outbox, registry):
    log = call_log(fake, outbox, registry, now=Clock())
    await log.add("user", "oi")
    await log.save(status="empty", finished=True)
    grant = registry.grant(log.record.id)
    assert {i.grant for i in outbox.pending()} == {grant}
    assert [i.path for i in outbox.pending()] == [
        f"/v1/node/calls/{log.record.id}/entries", f"/v1/node/calls/{log.record.id}",
        f"/v1/node/calls/{log.record.id}/finish",
    ]


async def test_push_arrives_after_the_finish_was_delivered(fake, outbox, registry):
    fake.push_targets[f"{ACCOUNT}:{DEVICE}"] = PUSH_KEY
    notifier = CloudPushNotifier(outbox, registry)
    log = call_log(fake, outbox, registry, now=Clock())
    async with running(outbox):
        await log.save(status="processing", ended_at=999.0)
    assert registry.ids() == []
    await log.save(status="delivered", attempts=0, finished=True)
    await notifier.call_finished(log.record)
    assert registry.grant(log.record.id) is None  # forgotten once the push is queued (I2)
    push = outbox.pending()[-1]
    assert push.kind == "push" and push.path == "/v1/node/push"
    assert push.body == {
        "event": "call.finished", "title": "Saved", "body": "Saved to Note.",
        "data": {"call_id": log.record.id, "status": "delivered", "error": None, "agent_id": log.record.agent_id},
        "collapse_id": log.record.id, "call": log.record.id,
    }
    async with running(outbox):
        pass
    assert len(fake.pushes) == 1
    sent = fake.pushes[0]
    assert sent["call"] == log.record.id and sent["device"] == DEVICE and sent["message"]["title"] == "Saved"


async def test_push_notifier_one_way_only(fake, outbox, registry, caplog):
    notifier = CloudPushNotifier(outbox, registry)
    conversation = call_log(fake, outbox, registry, call_type="conversation", now=Clock())
    await notifier.call_finished(replace_status(conversation.record, "ended"))
    assert len(outbox) == 0
    assert registry.grant(conversation.record.id) is None  # no push, and the grant is not kept

    unfinished = call_log(fake, outbox, registry, now=Clock())
    await notifier.call_finished(unfinished.record)  # still recording: no text, nothing to say
    assert len(outbox) == 0
    assert registry.grant(unfinished.record.id) is None

    one_way = call_log(fake, outbox, registry, now=Clock())

    no_grant = record_of(fake, open_call(fake)[0], status="empty")
    caplog.set_level(logging.INFO)
    await notifier.call_finished(no_grant)  # no grant in the registry: logged, nothing sent
    assert len(outbox) == 0
    assert no_grant.id in caplog.text

    await notifier.call_finished(replace_status(one_way.record, "empty"))
    assert [i.kind for i in outbox.pending()] == ["push"]
    assert outbox.pending()[0].body["title"] == "Nothing heard"


def replace_status(record: CallRecord, status: str) -> CallRecord:
    return replace(record, status=status)


async def test_self_hosted_history_start_refuses_scoped_history():
    history = History(None, HistoryCodec(KEY), account_scoped=True)
    with pytest.raises(RuntimeError):
        await history.start(None, None)  # refused before it looks at the agent
