"""PushNotifier (E6): the server asks the Cloud relay to notify a device when its one-way call ends, and a user's
management apps when a device asks for approval."""

import asyncio
import json
import logging
import time

import httpx
import pytest
import respx

from conftest import fake_config
from test_api import api_token, h, make_client
from wristcall import __version__
from wristcall.config import PushConfig
from wristcall.pairing import hash_secret
from wristcall.push import PushNotifier
from wristcall.storage import CallRecord, open_sqlite_storage
from wristcall.users import UserService

RELAY = "https://relay.test"
KEY_D1 = "wc_push_" + "1" * 43
KEY_D2 = "wc_push_" + "2" * 43
KEY_APP1 = "wc_push_" + "a" * 43
KEY_APP2 = "wc_push_" + "b" * 43


@pytest.fixture
def relay():
    with respx.mock(base_url=RELAY, assert_all_called=False) as mock:
        mock.post("/v1/push/send", name="send").mock(return_value=httpx.Response(202, json={"status": "sent"}))
        mock.delete("/v1/push/registrations/current", name="discard").mock(return_value=httpx.Response(204))
        yield mock


@pytest.fixture
async def storage():
    st = open_sqlite_storage(":memory:")
    users = UserService(st)
    user = await users.create("owner")
    await st.devices.create("d1", user.id, "Watch", hash_secret("device-1"), time.time())
    await st.devices.create("d2", user.id, "Other watch", hash_secret("device-2"), time.time())
    st.user = user  # the tests' user, at hand
    return st


@pytest.fixture
async def notifier(storage):
    async with httpx.AsyncClient() as http:
        yield PushNotifier(storage, PushConfig(relay_url=RELAY, timeout_s=0.5), http)


def call_record(storage, *, status="delivered", error=None, call_type="one-shot", device_id="d1", agent_name="Ana"):
    now = time.time()
    return CallRecord(
        id="c_0123456789abcdef", user_id=storage.user.id, agent_id="a_1", device_id=device_id, call_type=call_type,
        status=status, created_at=now, updated_at=now, error=error, agent_name=agent_name,
    )


def sent(relay):
    return [json.loads(c.request.content) for c in relay["send"].calls]


async def test_call_finished_reaches_the_calling_device(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    await storage.push.set(storage.user.id, KEY_D2, time.time(), device_id="d2")
    record = call_record(storage)
    await notifier.call_finished(record)
    assert relay["send"].call_count == 1
    req = relay.calls.last.request
    assert req.headers["authorization"] == f"Bearer {KEY_D1}"
    assert req.headers["user-agent"] == f"wristcall-server/{__version__}"
    assert json.loads(req.content) == {
        "event": "call.finished", "title": "Delivered", "body": "Ana got your message.",
        "data": {"call_id": record.id, "status": "delivered", "error": None, "agent_id": record.agent_id},
        "collapse_id": record.id,
    }


@pytest.mark.parametrize(
    "status, error, agent_name, title, body",
    [
        ("delivered", None, "Ana", "Delivered", "Ana got your message."),
        ("failed", "delivery_failed", "Ana", "Not delivered", "Ana did not get your message. It is saved in the history."),
        ("failed", "stt_failed", "Ana", "Not transcribed", "Your message to Ana could not be transcribed."),
        ("failed", "internal", "Ana", "Call failed", "Your call to Ana did not finish."),
        ("empty", None, "Ana", "Nothing heard", "Nothing was sent to Ana."),
        ("delivered", None, "", "Delivered", "your agent got your message."),
    ],
)
async def test_call_texts(storage, notifier, relay, status, error, agent_name, title, body):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    await notifier.call_finished(call_record(storage, status=status, error=error, agent_name=agent_name))
    (message,) = sent(relay)
    assert (message["title"], message["body"]) == (title, body)
    assert (message["data"]["status"], message["data"]["error"]) == (status, error)


async def test_long_names_fit_the_relay_limits(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    await storage.push.set(storage.user.id, KEY_APP1, time.time(), token_id=await _token(storage))
    for status, error in (("delivered", None), ("failed", "delivery_failed"), ("failed", "stt_failed"),
                          ("failed", "x"), ("empty", None)):
        await notifier.call_finished(call_record(storage, status=status, error=error, agent_name="A" * 255))
    await notifier.approval_requested(storage.user.id, "0042", "W" * 255, time.time() + 600)
    messages = sent(relay)
    assert len(messages) == 6
    for message in messages:
        assert 1 <= len(message["title"]) <= 100 and len(message["body"]) <= 300
    assert "A" * 40 in messages[0]["body"]  # shortened, not dropped


async def test_control_characters_never_reach_the_relay(storage, notifier, relay):
    # The Cloud refuses titles and bodies with control characters: the message would be lost.
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    await notifier.call_finished(call_record(storage, agent_name="An\x85a​"))
    (message,) = sent(relay)
    assert message["body"] == "Ana​ got your message."


async def test_conversation_calls_get_no_push(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    await notifier.call_finished(call_record(storage, status="ended", call_type="conversation"))
    assert not relay["send"].called


async def test_device_without_key_gets_no_push(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_D2, time.time(), device_id="d2")
    await notifier.call_finished(call_record(storage))
    await notifier.call_finished(call_record(storage, device_id=None))
    assert not relay.calls.called


async def test_revoked_device_gets_no_push(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    await storage.devices.revoke("d1", time.time())
    await notifier.call_finished(call_record(storage))
    assert not relay.calls.called


async def test_gone_key_is_forgotten(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    relay["send"].mock(return_value=httpx.Response(410, json={"error": "gone"}))
    assert await notifier.send(KEY_D1, "call.finished", "Delivered", "Ana got your message.", {}) is False
    assert await storage.push.for_device("d1") is None


@pytest.mark.parametrize(
    "outcome", [httpx.ConnectError("refused"), httpx.Response(500), httpx.Response(503), httpx.Response(429)]
)
async def test_relay_down_does_not_affect_the_call(storage, notifier, relay, outcome, caplog):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    relay["send"].mock(side_effect=outcome) if isinstance(outcome, Exception) else relay["send"].mock(return_value=outcome)
    assert await notifier.send(KEY_D1, "call.finished", "Delivered", "Ana got your message.", {}) is False
    await notifier.call_finished(call_record(storage))  # never raises
    assert await storage.push.for_device("d1") == KEY_D1  # only a 410 forgets the key
    expected = "ConnectError" if isinstance(outcome, Exception) else str(outcome.status_code)
    assert expected in caplog.text


async def test_relay_timeout_is_bounded(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(202)

    relay["send"].mock(side_effect=slow)
    relay["discard"].mock(side_effect=slow)
    started = time.monotonic()
    assert await notifier.send(KEY_D1, "call.finished", "Delivered", "Ana got your message.", {}) is False
    await notifier.call_finished(call_record(storage))
    await notifier.discard(KEY_D1)
    assert time.monotonic() - started < 2


async def _token(storage, name="app") -> str:
    record, _ = await UserService(storage).issue_token(storage.user.id, name)
    return record.id


async def test_approval_goes_to_the_users_apps(storage, notifier, relay):
    await storage.push.set(storage.user.id, KEY_APP1, time.time(), token_id=await _token(storage, "phone"))
    await storage.push.set(storage.user.id, KEY_APP2, time.time(), token_id=await _token(storage, "pwa"))
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    other = await UserService(storage).create("other")
    other_token, _ = await UserService(storage).issue_token(other.id, "other")
    await storage.push.set(other.id, "wc_push_" + "o" * 43, time.time(), token_id=other_token.id)
    expires_at = time.time() + 600
    await notifier.approval_requested(storage.user.id, "0042", "Wrist", expires_at)
    keys = sorted(c.request.headers["authorization"] for c in relay["send"].calls)
    assert keys == [f"Bearer {KEY_APP1}", f"Bearer {KEY_APP2}"]
    for message in sent(relay):
        assert message == {
            "event": "device.approval", "title": "New device",
            "body": '"Wrist" wants to use your account. Open the app to approve it.',
            "data": {"request_id": "0042", "device_name": "Wrist", "expires_at": expires_at},
        }


async def test_discard_is_best_effort(notifier, relay, caplog):
    await notifier.discard(KEY_D1)
    req = relay["discard"].calls.last.request
    assert req.headers["authorization"] == f"Bearer {KEY_D1}"
    relay["discard"].mock(return_value=httpx.Response(404))
    await notifier.discard(KEY_D1)
    relay["discard"].mock(side_effect=httpx.ConnectError("refused"))
    await notifier.discard(KEY_D1)  # never raises
    assert "ConnectError" in caplog.text


def test_replaced_key_is_discarded_at_the_relay():
    config = fake_config().model_copy(update={"push": PushConfig(relay_url=RELAY, timeout_s=0.5)})
    with respx.mock(base_url=RELAY, assert_all_called=False) as relay:
        discard = relay.delete("/v1/push/registrations/current").mock(return_value=httpx.Response(204))
        with make_client(config) as client:
            token = api_token(client)
            assert client.put("/v1/push", headers=h(token), json={"push_key": KEY_APP1}).status_code == 204
            assert client.put("/v1/push", headers=h(token), json={"push_key": KEY_APP2}).status_code == 204
            deadline = time.monotonic() + 2
            while not discard.called and time.monotonic() < deadline:
                time.sleep(0.01)
            assert discard.call_count == 1
            assert discard.calls.last.request.headers["authorization"] == f"Bearer {KEY_APP1}"
            assert client.delete("/v1/push", headers=h(token)).status_code == 204
            deadline = time.monotonic() + 2
            while discard.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert discard.calls.last.request.headers["authorization"] == f"Bearer {KEY_APP2}"


async def test_key_never_logged(storage, notifier, relay, caplog):
    caplog.set_level(logging.DEBUG)
    await storage.push.set(storage.user.id, KEY_D1, time.time(), device_id="d1")
    await storage.push.set(storage.user.id, KEY_APP1, time.time(), token_id=await _token(storage))
    for outcome in (httpx.Response(202), httpx.Response(500, text=KEY_D1), httpx.ConnectError(KEY_D1),
                    httpx.Response(410, text=KEY_D1)):
        if isinstance(outcome, Exception):
            relay["send"].mock(side_effect=outcome)
            relay["discard"].mock(side_effect=outcome)
        else:
            relay["send"].mock(return_value=outcome)
            relay["discard"].mock(return_value=outcome)
        await notifier.call_finished(call_record(storage))
        await notifier.approval_requested(storage.user.id, "0042", "Wrist", time.time() + 600)
        await notifier.discard(KEY_D1)
    assert relay["send"].called
    for key in (KEY_D1, KEY_APP1, "1" * 43, "a" * 43):
        assert key not in caplog.text
