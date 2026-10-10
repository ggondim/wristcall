import asyncio
import base64
import json
import logging
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from conftest import CLIENTS, ISSUER, mongo_url, serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig, ConfigError, config_from_env
from wristcall_cloud.push.channels import FakeChannel
from wristcall_cloud.push.limits import RegistrationLimiter, SendLimiter
from wristcall_cloud.push.registry import key_id, new_push_key, purge_idle_loop
from wristcall_cloud.store import Store

TOPIC = "io.github.ggondim.wristcall"
APNS = {"platform": "apns", "token": "ab" * 32, "topic": "io.github.ggondim.wristcall",
        "environment": "sandbox", "label": "Home", "tag": "srv-1", "events": ["call.finished"]}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


ENDPOINT = "https://fcm.googleapis.com/fcm/send/s3cr3t-endpoint-id"
# A browser's key: a point of the P-256 curve (uncompressed, 65 bytes).
POINT = ec.derive_private_key(12345, ec.SECP256R1()).public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
P256DH = b64(POINT)
AUTH = b64(b"0123456789abcdef")
WEBPUSH = {"platform": "webpush", "subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": P256DH, "auth": AUTH}},
           "label": "Home", "events": ["device.approval"]}


@pytest.fixture
def fake() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def config() -> CloudConfig:
    # The per-IP registration limit has its own test; here it must not get in the way of many registrations.
    return CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,),
                       registrations_per_minute_per_ip=1000)


@pytest.fixture
def app(config, mongo_db, fake_verifier, fake):
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier,
                      channels={"apns": fake, "webpush": fake})


def register(client, body=APNS):
    r = client.post("/v1/push/registrations", json=body)
    assert r.status_code == 201
    return r.json()["push_key"]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_register_and_send(client, fake):
    key = register(client)
    assert key.startswith("wc_push_") and len(key) == 51
    r = client.post("/v1/push/send", json={"event": "call.finished", "title": "Delivered", "body": "Ana got it",
                                           "data": {"call_id": "c_1"}}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 202
    reg, msg = fake.sent[-1]
    assert reg["channel"] == "ab" * 32 and msg.subtitle == "Home" and msg.tag == "srv-1"
    assert msg.data == {"call_id": "c_1"}


def test_registration_response_and_document(client, mongo_db):
    r = client.post("/v1/push/registrations", json=APNS)
    assert r.status_code == 201
    body = r.json()
    assert body == {"push_key": body["push_key"], "platform": "apns", "label": "Home", "tag": "srv-1",
                    "events": ["call.finished"]}
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        docs = list(sync[mongo_db.name]["push_registrations"].find())
    assert len(docs) == 1
    doc = docs[0]
    # Only the hash of the key is stored.
    assert doc["_id"] == key_id(body["push_key"]) and body["push_key"] not in json.dumps(doc, default=str)
    assert doc["platform"] == "apns" and doc["channel"] == "ab" * 32
    assert doc["topic"] == TOPIC and doc["environment"] == "sandbox"
    assert doc["last_sent_at"] == doc["created_at"]


def test_webpush_registration(client, fake, mongo_db):
    key = register(client, WEBPUSH)
    r = client.get("/v1/push/registrations/current", headers=auth(key))
    assert r.json() == {"platform": "webpush", "label": "Home", "tag": "", "events": ["device.approval"]}
    assert client.post("/v1/push/send", json={"event": "device.approval", "title": "New device"},
                       headers=auth(key)).status_code == 202
    reg, msg = fake.sent[-1]
    assert reg["channel"] == ENDPOINT and reg["p256dh"] == P256DH and reg["auth"] == AUTH
    assert msg.event == "device.approval" and msg.body == "" and msg.data == {} and msg.ttl_s == 3600
    assert msg.collapse_id is None and msg.tag == ""


def test_label_is_forced_on_every_message(client, fake):
    key = register(client)
    h = {"Authorization": f"Bearer {key}"}
    # "subtitle" and "tag" are not fields the sender controls: 422, nothing sent
    for extra in ({"subtitle": "Bank"}, {"tag": "srv-2"}):
        r = client.post("/v1/push/send", json={"event": "test", "title": "t", "data": {}, **extra}, headers=h)
        assert r.status_code == 422
    assert fake.sent == []
    client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=h)
    assert fake.sent[-1][1].subtitle == "Home" and fake.sent[-1][1].tag == "srv-1"


def test_events_are_limited_to_the_registration(client, fake):
    key = register(client)  # events: ["call.finished"]
    r = client.post("/v1/push/send", json={"event": "device.approval", "title": "t"},
                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 422 and fake.sent == []


def test_current_registration(client):
    key = register(client)
    r = client.get("/v1/push/registrations/current", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json() == {"platform": "apns", "label": "Home", "tag": "srv-1",
                                                 "events": ["call.finished"]}
    assert client.get("/v1/push/registrations/current",
                      headers={"Authorization": "Bearer wc_push_" + "x" * 43}).status_code == 410


def test_database_error_is_503_not_410(client, app, monkeypatch):
    key = register(client)
    collection = app.state.store.push_registrations

    async def broken(*_args, **_kwargs):
        raise PyMongoError("connection refused")

    monkeypatch.setattr(collection, "find_one", broken)
    for r in (
        client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key)),
        client.get("/v1/push/registrations/current", headers=auth(key)),
    ):
        assert r.status_code == 503
        assert r.json() == {"error": "push_unavailable", "message": "push is unavailable; try again later"}
    monkeypatch.setattr(collection, "insert_one", broken)
    monkeypatch.setattr(collection, "delete_one", broken)
    assert client.post("/v1/push/registrations", json=APNS).status_code == 503
    assert client.delete("/v1/push/registrations/current", headers=auth(key)).status_code == 503


def test_registration_rate_limit_per_ip(mongo_db, fake_verifier, fake):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,),
                      client_ip_header="X-Forwarded-For")
    app = create_app(cfg, store=Store(mongo_db), verifier=fake_verifier, channels={"apns": fake})
    for c in serve(app, mongo_db):
        # The proxy appends the address it saw: the last value counts, whatever the client put before it.
        same_ip = [{"X-Forwarded-For": f"10.9.9.{i}, 203.0.113.7"} for i in range(11)]
        codes = [c.post("/v1/push/registrations", json=APNS, headers=h).status_code for h in same_ip]
        assert codes[:10] == [201] * 10 and codes[10] == 429
        r = c.post("/v1/push/registrations", json=APNS, headers={"X-Forwarded-For": "203.0.113.7"})
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert 1 <= int(r.headers["Retry-After"]) <= 60
        assert c.post("/v1/push/registrations", json=APNS,
                      headers={"X-Forwarded-For": "203.0.113.8"}).status_code == 201


def test_registration_rate_limit_ignores_the_header_unless_configured(mongo_db, fake_verifier, fake):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,))
    app = create_app(cfg, store=Store(mongo_db), verifier=fake_verifier, channels={"apns": fake})
    for c in serve(app, mongo_db):
        codes = [c.post("/v1/push/registrations", json=APNS, headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
                 for i in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


@pytest.fixture
async def store(mongo_db):
    s = Store(mongo_db)
    await s.ensure_indexes()
    yield s
    await mongo_db.client.close()


def _reg(channel: str) -> dict:
    return {"platform": "apns", "channel": channel, "topic": TOPIC, "environment": "sandbox", "label": "Home",
            "tag": "", "events": ["call.finished"]}


async def test_idle_registrations_are_purged(store):
    old, recent, used = new_push_key(), new_push_key(), new_push_key()
    await store.add_registration(key_id(old), _reg("aa" * 32), now=1000.0)
    await store.add_registration(key_id(recent), _reg("bb" * 32), now=5000.0)
    await store.add_registration(key_id(used), _reg("cc" * 32), now=1000.0)
    await store.touch_registration(key_id(used), now=6000.0)
    assert await store.purge_idle_registrations(before=2000.0) == 1
    assert await store.get_registration(key_id(old)) is None
    assert await store.get_registration(key_id(recent)) is not None
    assert await store.get_registration(key_id(used)) is not None
    assert await store.purge_idle_registrations(before=2000.0) == 0


async def test_purge_loop_runs_on_its_period():
    calls: list[float] = []
    sleeps: list[float] = []

    class FakeStore:
        async def purge_idle_registrations(self, before: float) -> int:
            calls.append(before)
            if len(calls) == 2:
                raise PyMongoError("down")  # a failed round is logged; the loop goes on
            return 1

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await purge_idle_loop(FakeStore(), every_s=21600, idle_s=180 * 86400, sleep=sleep, now=lambda: 2e7)
    assert sleeps == [21600] * 4
    assert calls == [2e7 - 180 * 86400] * 3


def test_send_rate_limit(client, fake):
    key = register(client)
    h = {"Authorization": f"Bearer {key}"}
    codes = [client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=h).status_code for _ in range(31)]
    assert codes[:30] == [202] * 30 and codes[30] == 429


def test_send_rate_limit_response(client, fake):
    key = register(client)
    for _ in range(30):
        client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key))
    r = client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key))
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert len(fake.sent) == 30
    # Another key has its own budget.
    other = register(client)
    assert client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(other)).status_code == 202


def test_send_limiter_windows():
    clock = [0.0]
    limiter = SendLimiter(per_minute=2, per_day=3, now=lambda: clock[0])
    assert limiter.allow("k") is None and limiter.allow("k") is None
    assert limiter.allow("k") == pytest.approx(60.0)
    assert limiter.allow("other") is None
    clock[0] = 59.5
    assert limiter.allow("k") == pytest.approx(0.5)
    clock[0] = 60.0
    assert limiter.allow("k") is None  # third of the day
    clock[0] = 130.0
    assert limiter.allow("k") == pytest.approx(86400 - 130.0)  # the day is spent
    clock[0] = 86400.0
    assert limiter.allow("k") is None


def test_limiters_forget_expired_keys():
    clock = [0.0]
    limiter = RegistrationLimiter(per_minute=1, now=lambda: clock[0])
    for i in range(999):
        assert limiter.allow(f"ip-{i}") is None
    clock[0] = 61.0
    limiter.allow("ip-new")  # the 1000th call sweeps the windows that are over
    assert limiter.size() == 1
    sends = SendLimiter(per_minute=1, per_day=10, now=lambda: clock[0])
    for i in range(1000):
        sends.allow(f"k-{i}")
    clock[0] = 86400 + 62.0
    for i in range(1000):
        sends.allow("same")
    assert sends.size() == 1


def test_unknown_key_is_gone(client):
    send = {"event": "test", "title": "t"}
    unknown = new_push_key()
    for header in (f"Bearer {unknown}", "Bearer wc_push_short", "Bearer not-a-push-key", f"Bearer {unknown}x"):
        r = client.post("/v1/push/send", json=send, headers={"Authorization": header})
        assert r.status_code == 410 and r.json()["error"] == "gone"
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        r = client.post("/v1/push/send", json=send, headers=headers)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"
        assert client.get("/v1/push/registrations/current", headers=headers).status_code == 401
        assert client.delete("/v1/push/registrations/current", headers=headers).status_code == 401


def test_unknown_key_is_gone_even_with_a_bad_body(client):
    # A key the Cloud does not know is forgotten by the server whatever it was sending.
    r = client.post("/v1/push/send", json={"event": "nope"}, headers=auth(new_push_key()))
    assert r.status_code == 410


def test_gone_channel_deletes_registration(client, fake):
    key = register(client)
    other = register(client, {**APNS, "token": "cd" * 32})
    fake.gone = {"ab" * 32}
    send = {"event": "test", "title": "t"}
    assert client.post("/v1/push/send", json=send, headers=auth(key)).status_code == 410
    assert client.post("/v1/push/send", json=send, headers=auth(key)).status_code == 410
    assert client.delete("/v1/push/registrations/current", headers=auth(key)).status_code == 404
    assert client.post("/v1/push/send", json=send, headers=auth(other)).status_code == 202


def test_unavailable_channel_is_503(client, fake):
    key = register(client)
    fake.down = True
    r = client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key))
    assert r.status_code == 503 and r.json()["error"] == "push_unavailable"
    assert client.get("/v1/push/registrations/current", headers=auth(key)).status_code == 200
    fake.down = False
    assert client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key)).status_code == 202


def test_platform_without_channel_now_is_503(mongo_db, fake_verifier, fake):
    # A registration made while a channel existed: if the channel goes away, the key is not gone.
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,))
    for c in serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier, channels={"apns": fake}), mongo_db):
        key = register(c)
        c.app.state.channels = {}
        assert c.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key)).status_code == 503


def test_send_updates_last_sent_at(client, mongo_db):
    key = register(client)
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        col = sync[mongo_db.name]["push_registrations"]
        before = col.find_one({"_id": key_id(key)})["last_sent_at"]
        time.sleep(0.01)
        client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key))
        assert col.find_one({"_id": key_id(key)})["last_sent_at"] > before


def test_delete_registration(client):
    key = register(client)
    assert client.delete("/v1/push/registrations/current", headers=auth(key)).status_code == 204
    assert client.get("/v1/push/registrations/current", headers=auth(key)).status_code == 410
    assert client.delete("/v1/push/registrations/current", headers=auth(key)).status_code == 404
    for header in (f"Bearer {new_push_key()}", "Bearer wc_push_short"):
        r = client.delete("/v1/push/registrations/current", headers={"Authorization": header})
        assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_registration_limit_per_channel(client):
    keys = [register(client) for _ in range(21)]
    assert client.get("/v1/push/registrations/current", headers=auth(keys[0])).status_code == 410
    for key in keys[1:]:
        assert client.get("/v1/push/registrations/current", headers=auth(key)).status_code == 200
    # Another device token keeps its own registrations.
    assert client.get("/v1/push/registrations/current",
                      headers=auth(register(client, {**APNS, "token": "cd" * 32}))).status_code == 200
    assert client.get("/v1/push/registrations/current", headers=auth(keys[1])).status_code == 200


def test_indexes_are_created(client, mongo_db):
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        keys = [tuple(ix["key"].items()) for ix in sync[mongo_db.name]["push_registrations"].list_indexes()]
    assert (("channel", 1), ("created_at", 1)) in keys
    assert (("last_sent_at", 1),) in keys


def _webpush(**subscription) -> dict:
    return {**WEBPUSH, "subscription": {**WEBPUSH["subscription"], **subscription}}


def _keys(**keys) -> dict:
    return _webpush(keys={"p256dh": P256DH, "auth": AUTH, **keys})


@pytest.mark.parametrize("body", [
    {**APNS, "topic": "com.example.other"},
    {**APNS, "token": "zz" * 32}, {**APNS, "token": "ab" * 31}, {**APNS, "token": "a" * 65}, {**APNS, "token": "ab" * 101},
    {**APNS, "token": 123},
    {**APNS, "environment": "staging"}, {k: v for k, v in APNS.items() if k != "environment"},
    {**APNS, "label": ""}, {**APNS, "label": "   "}, {**APNS, "label": "x" * 65}, {**APNS, "label": "\n"},
    {**APNS, "label": "Ho\nme"}, {**APNS, "label": 1}, {k: v for k, v in APNS.items() if k != "label"},
    {**APNS, "platform": "fcm"}, {**APNS, "platform": None}, {k: v for k, v in APNS.items() if k != "platform"},
    {**APNS, "extra": 1}, {**APNS, "subscription": WEBPUSH["subscription"]},
    {**APNS, "events": []}, {**APNS, "events": ["call.finished", "call.finished"]}, {**APNS, "events": ["test"]},
    {**APNS, "events": ["other"]}, {**APNS, "events": "call.finished"}, {k: v for k, v in APNS.items() if k != "events"},
    {**APNS, "tag": "srv 1"}, {**APNS, "tag": "x" * 65}, {**APNS, "tag": 1},
    {**WEBPUSH, "token": "ab" * 32}, {**WEBPUSH, "subscription": "x"},
    _webpush(endpoint="http://fcm.googleapis.com/fcm/send/x"), _webpush(endpoint="fcm.googleapis.com/x"),
    _webpush(endpoint="https://fcm.googleapis.com/a b"), _webpush(endpoint=1), _webpush(extra=1),
    _webpush(endpoint="https://" + "a" * 2050),
    _keys(p256dh=b64(b"\x04" + bytes(63))), _keys(p256dh=b64(b"\x05" + bytes(64))), _keys(p256dh="***"),
    # 65 bytes starting with 0x04, but not a point of the curve.
    _keys(p256dh=b64(b"\x04" + bytes(range(64)))), _keys(p256dh=b64(b"\x04" + bytes(64))),
    _keys(p256dh=b64(POINT[:-1] + bytes([POINT[-1] ^ 1]))),
    _keys(auth=b64(bytes(15))), _keys(auth=1), _keys(extra="x"), _webpush(keys=None),
    [], "text",
])
def test_invalid_registration_is_422(client, body):
    r = client.post("/v1/push/registrations", json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid"


def test_registration_accepts_a_browser_subscription(client):
    # PushSubscription.toJSON() has expirationTime as well; padded base64url is accepted.
    padded = base64.urlsafe_b64encode(POINT).decode()
    body = _webpush(expirationTime=None, keys={"p256dh": padded, "auth": AUTH})
    assert client.post("/v1/push/registrations", json=body).status_code == 201
    upper = {**APNS, "token": "AB" * 32, "label": "  Home  "}
    key = register(client, upper)
    assert client.get("/v1/push/registrations/current", headers=auth(key)).json()["label"] == "Home"


@pytest.mark.parametrize("body", [
    {"event": "other", "title": "t"}, {"event": "device.approval", "title": "t"}, {"title": "t"}, {"event": 1, "title": "t"},
    {"event": "test", "title": ""}, {"event": "test", "title": "x" * 101}, {"event": "test"}, {"event": "test", "title": 1},
    {"event": "test", "title": "t", "body": "x" * 301}, {"event": "test", "title": "t", "body": 1},
    {"event": "test", "title": "t", "data": []}, {"event": "test", "title": "t", "data": "x"},
    {"event": "test", "title": "t", "data": {"x": "a" * 1017}},  # 1025 bytes
    {"event": "test", "title": "t", "ttl_s": -1}, {"event": "test", "title": "t", "ttl_s": 86401},
    {"event": "test", "title": "t", "ttl_s": 1.5}, {"event": "test", "title": "t", "ttl_s": True},
    {"event": "test", "title": "t", "collapse_id": "x" * 65}, {"event": "test", "title": "t", "collapse_id": ""},
    {"event": "test", "title": "t", "collapse_id": "a b"},
    {"event": "test", "title": "t", "extra": 1}, {"event": "test", "title": "t\u0007"},
    [],
])
def test_invalid_message_is_422(client, fake, body):
    key = register(client)
    r = client.post("/v1/push/send", json=body, headers=auth(key))
    assert r.status_code == 422 and r.json()["error"] == "invalid"
    assert fake.sent == []


def test_message_limits_are_inclusive(client, fake):
    key = register(client)
    data = {"x": "a" * 1016}
    assert len(json.dumps(data, separators=(",", ":")).encode()) == 1024
    body = {"event": "call.finished", "title": "x" * 100, "body": "line 1\nline 2" + "y" * 287, "data": data,
            "ttl_s": 86400, "collapse_id": "c" * 64}
    assert client.post("/v1/push/send", json=body, headers=auth(key)).status_code == 202
    msg = fake.sent[-1][1]
    assert msg.ttl_s == 86400 and msg.collapse_id == "c" * 64 and msg.title == "x" * 100
    assert client.post("/v1/push/send", json={"event": "test", "title": "t", "ttl_s": 0},
                       headers=auth(key)).status_code == 202
    assert fake.sent[-1][1].ttl_s == 0


def test_keys_and_channels_never_logged(client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    apns_key = register(client)
    web_key = register(client, WEBPUSH)
    for key in (apns_key, web_key):
        client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key))
        client.get("/v1/push/registrations/current", headers=auth(key))
    fake.down = True
    client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(apns_key))
    fake.down = False
    fake.gone = {ENDPOINT}
    client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(web_key))
    client.delete("/v1/push/registrations/current", headers=auth(apns_key))
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("push registered" in m for m in messages)
    secrets = (apns_key, web_key, key_id(apns_key), key_id(web_key), "ab" * 32, ENDPOINT, "s3cr3t", P256DH, AUTH)
    assert not any(s in m for s in secrets for m in messages)


def test_responses_never_carry_the_channel(client):
    key = register(client)
    r = client.get("/v1/push/registrations/current", headers=auth(key))
    assert "ab" * 32 not in r.text and TOPIC not in r.text
    r = client.post("/v1/push/registrations", json=WEBPUSH)
    assert ENDPOINT not in r.text and P256DH not in r.text


def test_registration_without_channel_is_404(mongo_db, fake_verifier, fake):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,))
    for c in serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier, channels={"apns": fake}), mongo_db):
        r = c.post("/v1/push/registrations", json=WEBPUSH)
        assert r.status_code == 404 and r.json()["error"] == "not_configured"
        assert c.post("/v1/push/registrations", json=APNS).status_code == 201


def test_no_channels_means_not_configured(mongo_db, fake_verifier):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,))
    for c in serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier), mongo_db):
        for body in (APNS, WEBPUSH):
            r = c.post("/v1/push/registrations", json=body)
            assert r.status_code == 404 and r.json()["error"] == "not_configured"
        assert c.get("/v1/config").json()["push"] == {"apns": False, "webpush": False, "vapid_public_key": None,
                                                      "apns_topics": [TOPIC]}


def test_config_reports_push(client):
    assert client.get("/v1/config").json()["push"] == {
        "apns": True, "webpush": True, "vapid_public_key": None, "apns_topics": [TOPIC],
    }


def test_push_fake_uses_one_fake_channel(mongo_db, fake_verifier):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,),
                      public_url="http://127.0.0.1:8090", push_fake=True)
    app = create_app(cfg, store=Store(mongo_db), verifier=fake_verifier)
    fake = app.state.channels["apns"]
    assert isinstance(fake, FakeChannel) and app.state.channels["webpush"] is fake
    for c in serve(app, mongo_db):
        key = register(c)
        assert c.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=auth(key)).status_code == 202
        assert c.get("/v1/config").json()["push"]["apns"] is True
    assert len(fake.sent) == 1


@pytest.mark.parametrize("public_url", [None, "https://cloud.example", "http://192.168.0.10:8090"])
def test_push_fake_needs_a_loopback_public_url(mongo_db, fake_verifier, public_url):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), public_url=public_url,
                      push_fake=True)
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_PUSH_FAKE"):
        create_app(cfg, store=Store(mongo_db), verifier=fake_verifier)


BASE_ENV = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017", "WRISTCALL_CLOUD_CLIENT_IOS": "client-ios"}


def test_push_config_from_env():
    plain = config_from_env(BASE_ENV)
    assert (plain.apns_topics, plain.push_per_minute, plain.push_per_day) == ((), 30, 500)
    assert (plain.registrations_per_minute_per_ip, plain.client_ip_header, plain.push_idle_days) == (10, None, 180)
    assert plain.push_fake is False and plain.push_cleanup_every_s == 21600
    cfg = config_from_env({
        **BASE_ENV,
        "WRISTCALL_CLOUD_APNS_TOPICS": " io.github.ggondim.wristcall , io.github.ggondim.wristcall.ios,",
        "WRISTCALL_CLOUD_PUSH_PER_MINUTE": "5",
        "WRISTCALL_CLOUD_PUSH_PER_DAY": "50",
        "WRISTCALL_CLOUD_REGISTRATIONS_PER_MINUTE": "3",
        "WRISTCALL_CLOUD_CLIENT_IP_HEADER": "X-Forwarded-For",
        "WRISTCALL_CLOUD_PUSH_IDLE_DAYS": "30",
    })
    assert cfg.apns_topics == ("io.github.ggondim.wristcall", "io.github.ggondim.wristcall.ios")
    assert (cfg.push_per_minute, cfg.push_per_day, cfg.registrations_per_minute_per_ip) == (5, 50, 3)
    assert cfg.client_ip_header == "X-Forwarded-For" and cfg.push_idle_days == 30


@pytest.mark.parametrize("overrides", [
    {"WRISTCALL_CLOUD_APNS_TOPICS": "io.example/s3cr3t"},
    {"WRISTCALL_CLOUD_CLIENT_IP_HEADER": "X Forwarded s3cr3t"},
    {"WRISTCALL_CLOUD_PUSH_PER_MINUTE": "s3cr3t"},
    {"WRISTCALL_CLOUD_PUSH_PER_DAY": "0"},
    {"WRISTCALL_CLOUD_REGISTRATIONS_PER_MINUTE": "-1"},
    {"WRISTCALL_CLOUD_PUSH_IDLE_DAYS": "x"},
    {"WRISTCALL_CLOUD_PUSH_FAKE": "maybe"},
    {"WRISTCALL_CLOUD_PUSH_FAKE": "1"},  # no public URL
])
def test_push_config_errors(overrides):
    with pytest.raises(ConfigError) as e:
        config_from_env({**BASE_ENV, **overrides})
    name = next(iter(overrides))
    assert name in str(e.value) and "s3cr3t" not in str(e.value)


def test_push_fake_from_env(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    env = {**BASE_ENV, "WRISTCALL_CLOUD_PUSH_FAKE": "1", "WRISTCALL_CLOUD_SIGNING_KEY": pem}
    assert config_from_env({**env, "WRISTCALL_CLOUD_PUBLIC_URL": "http://127.0.0.1:8090"}).push_fake is True
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_PUSH_FAKE"):
        config_from_env({**env, "WRISTCALL_CLOUD_PUBLIC_URL": "https://cloud.example"})



def test_lone_surrogates_are_422_not_500(client, fake):
    # "\ud800" is valid JSON but not encodable text: it must not reach the database or the channel.
    headers = {"Content-Type": "application/json"}
    reg = json.dumps({**APNS, "label": "@@"}).replace("@@", "\\ud800").encode()
    r = client.post("/v1/push/registrations", content=reg, headers=headers)
    assert r.status_code == 422 and r.json()["error"] == "invalid"
    key = register(client)
    for field, value in (("title", '"\\ud800"'), ("body", '"\\ud800"'), ("data", '{"x": "\\ud800"}')):
        body = '{"event": "test", "title": "t", "%s": %s}' % (field, value) if field != "title" else (
            '{"event": "test", "title": %s}' % value)
        r = client.post("/v1/push/send", content=body.encode(), headers={**headers, **auth(key)})
        assert r.status_code == 422 and r.json()["error"] == "invalid"
    assert fake.sent == []


def test_nan_and_infinity_in_data_are_422(client, fake):
    # Python's JSON reader takes NaN and Infinity, which are not JSON: they must not reach a channel.
    key = register(client)
    headers = {"Content-Type": "application/json", **auth(key)}
    for value in ("NaN", "Infinity", "-Infinity"):
        body = '{"event": "test", "title": "t", "data": {"x": %s}}' % value
        r = client.post("/v1/push/send", content=body.encode(), headers=headers)
        assert r.status_code == 422 and r.json()["error"] == "invalid"
    assert fake.sent == []


def test_shutdown_closes_clients_when_the_purge_task_failed(mongo_db, fake_verifier, monkeypatch):
    import httpx

    import wristcall_cloud.app as app_module

    async def broken_loop(*args, **kwargs):
        raise RuntimeError("purge task died")

    closed: list[str] = []

    class Mongo:
        async def close(self) -> None:
            closed.append("mongo")

    original_aclose = httpx.AsyncClient.aclose

    async def aclose(self) -> None:
        closed.append("http")
        await original_aclose(self)

    monkeypatch.setattr(app_module, "purge_idle_loop", broken_loop)
    monkeypatch.setattr(app_module, "open_store", lambda config: (Mongo(), Store(mongo_db)))
    monkeypatch.setattr(httpx.AsyncClient, "aclose", aclose)
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS))
    for c in serve(create_app(cfg), mongo_db):  # no verifier: the app owns its HTTP client
        assert c.get("/v1/health").status_code == 200
    assert "mongo" in closed and "http" in closed


def test_shutdown_closes_everything_when_a_close_fails(mongo_db, fake_verifier, monkeypatch):
    import httpx

    import wristcall_cloud.app as app_module
    from wristcall_cloud.push.webpush import Vapid

    closed: list[str] = []

    class Mongo:
        async def close(self) -> None:
            closed.append("mongo")
            raise RuntimeError("mongo close failed")

    original_aclose = httpx.AsyncClient.aclose
    clients: dict[int, str] = {}

    async def aclose(self) -> None:
        closed.append(clients[id(self)])
        await original_aclose(self)
        if clients[id(self)] == "webpush":
            raise RuntimeError("close failed")

    vapid_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    apns_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    monkeypatch.setattr(app_module, "open_store", lambda config: (Mongo(), Store(mongo_db)))
    monkeypatch.setattr(httpx.AsyncClient, "aclose", aclose)
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,),
                      vapid_private_pem=vapid_pem, vapid_subject="mailto:ops@example.com",
                      apns_key_pem=apns_pem, apns_key_id="KEY123", apns_team_id="TEAM456")
    app = create_app(cfg)  # no verifier: the app owns its HTTP client as well
    clients[id(app.state.verifier._http)] = "oidc"
    clients[id(app.state.channels["webpush"].http)] = "webpush"
    clients[id(app.state.channels["apns"].http)] = "apns"
    assert isinstance(app.state.channels["webpush"].vapid, Vapid)
    with pytest.raises(RuntimeError):
        for c in serve(app, mongo_db):
            assert c.get("/v1/health").status_code == 200
    assert sorted(closed) == ["apns", "mongo", "oidc", "webpush"]


async def test_purge_loop_survives_any_error():
    calls: list[float] = []
    sleeps: list[float] = []

    class FakeStore:
        async def purge_idle_registrations(self, before: float) -> int:
            calls.append(before)
            if len(calls) == 1:
                raise RuntimeError("unexpected")
            return 0

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await purge_idle_loop(FakeStore(), every_s=60, idle_s=1, sleep=sleep, now=lambda: 100.0)
    assert len(calls) == 2


async def test_new_registration_is_never_evicted(store):
    # A registration older than the newest 20 (clock moved back) must not be the one that goes.
    keys = [new_push_key() for _ in range(20)]
    for i, key in enumerate(keys):
        await store.add_registration(key_id(key), _reg("aa" * 32), now=1000.0 + i)
    newest = new_push_key()
    await store.add_registration(key_id(newest), _reg("aa" * 32), now=1.0)
    assert await store.get_registration(key_id(newest)) is not None
    assert await store.get_registration(key_id(keys[0])) is None
    for key in keys[1:]:
        assert await store.get_registration(key_id(key)) is not None


def test_registration_rate_limit_reads_every_header_line(mongo_db, fake_verifier, fake):
    # A proxy may add its own header line instead of appending to the client's: the last line's last value counts.
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,),
                      client_ip_header="X-Forwarded-For")
    app = create_app(cfg, store=Store(mongo_db), verifier=fake_verifier, channels={"apns": fake})
    for c in serve(app, mongo_db):
        codes = [
            c.post("/v1/push/registrations", json=APNS,
                   headers=[("X-Forwarded-For", f"10.9.9.{i}"), ("X-Forwarded-For", "203.0.113.7")]).status_code
            for i in range(11)
        ]
    assert codes[:10] == [201] * 10 and codes[10] == 429
