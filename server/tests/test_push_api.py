import asyncio
import logging
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import fake_config
from test_api import api_token, device_token, h, make_client, run
from wristcall.auth import Authenticator
from wristcall.config import PushConfig
from wristcall.pairing import hash_secret
from wristcall.push import push_router
from wristcall.storage import open_sqlite_storage

KEY_A = "wc_push_" + "A" * 43
KEY_B = "wc_push_" + "B" * 43
KEY_C = "wc_push_" + "C" * 43


def push_config():
    return fake_config().model_copy(update={"push": PushConfig(relay_url="https://cloud.example.com/")})


@pytest.fixture
def client():
    with make_client(push_config()) as c:
        yield c


def put(client, token, key):
    return client.put("/v1/push", headers=h(token), json={"push_key": key})


def stored_for_device(client, token):
    device = run(client.app.state.storage.devices.by_token(hash_secret(token)))
    return run(client.app.state.storage.push.for_device(device.id))


def test_device_sets_and_clears_its_key(client):
    api_token(client)
    token = device_token(client)
    r = put(client, token, KEY_A)
    assert r.status_code == 204 and r.content == b""
    assert stored_for_device(client, token) == KEY_A
    assert put(client, token, KEY_B).status_code == 204
    assert stored_for_device(client, token) == KEY_B
    assert client.delete("/v1/push", headers=h(token)).status_code == 204
    assert stored_for_device(client, token) is None
    r = client.delete("/v1/push", headers=h(token))
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_api_token_sets_its_key(client):
    token = api_token(client)
    user = run(client.app.state.storage.users.by_handle("owner"))
    assert put(client, token, KEY_A).status_code == 204
    assert run(client.app.state.storage.push.for_apps(user.id)) == [KEY_A]
    assert client.delete("/v1/push", headers=h(token)).status_code == 204
    assert run(client.app.state.storage.push.for_apps(user.id)) == []
    assert client.delete("/v1/push", headers=h(token)).status_code == 404


def test_unauthorized(client):
    assert client.put("/v1/push", json={"push_key": KEY_A}).status_code == 401
    assert put(client, "bogus", KEY_A).status_code == 401
    assert client.delete("/v1/push").status_code == 401
    assert client.delete("/v1/push", headers=h("bogus")).status_code == 401


def test_revoked_token_is_unauthorized(client):
    token = api_token(client)
    user = run(client.app.state.storage.users.by_handle("owner"))
    (record,) = run(client.app.state.storage.tokens.list(user.id))
    run(client.app.state.storage.tokens.revoke(record.id, time.time()))
    assert put(client, token, KEY_A).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"push_key": "wc_pusx_" + "A" * 43},
        {"push_key": "wc_push_" + "A" * 42},
        {"push_key": "wc_push_" + "A" * 44},
        {"push_key": "wc_push_" + "A" * 42 + " "},
        {"push_key": "wc_push_" + "A" * 42 + "\n"},
        {"push_key": 12345},
        {"push_key": None},
        {"other": KEY_A},
        [KEY_A],
        "text",
    ],
)
def test_bad_key_is_422(client, body):
    token = api_token(client)
    r = client.put("/v1/push", headers=h(token), json=body)
    assert r.status_code == 422 and r.json()["error"] == "invalid"
    assert "A" * 20 not in r.text


def test_invalid_json_is_422(client):
    token = api_token(client)
    r = client.put("/v1/push", headers=h(token), content=b"{nope")
    assert r.status_code == 422


def test_push_not_configured():
    with make_client() as c:
        token = api_token(c)
        for r in (put(c, token, KEY_A), c.delete("/v1/push", headers=h(token)), c.delete("/v1/push")):
            assert r.status_code == 404 and r.json()["error"] == "not_configured"


def test_health_reports_relay(client):
    assert client.get("/v1/health").json()["push"] == {"relay": "https://cloud.example.com"}
    with make_client() as c:
        assert c.get("/v1/health").json()["push"] is None


def test_push_key_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    token = api_token(client)
    r = put(client, token, KEY_A)
    put(client, token, KEY_B)
    client.delete("/v1/push", headers=h(token))
    assert KEY_A not in caplog.text and KEY_B not in caplog.text
    assert KEY_A not in r.text


def test_another_users_clients_are_independent(client):
    first = api_token(client)
    second = api_token(client, "other")
    assert put(client, first, KEY_A).status_code == 204
    assert put(client, second, KEY_B).status_code == 204
    other = run(client.app.state.storage.users.by_handle("other"))
    assert run(client.app.state.storage.push.for_apps(other.id)) == [KEY_B]


def settle(calls, count, timeout=2.0):
    """The hook runs in the background: waits until it has been called `count` times."""
    deadline = time.monotonic() + timeout
    while len(calls) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)  # room for an unexpected extra call


# on_discarded hook: wired in a bare app so the callback can be observed.
@pytest.fixture
def hooked():
    storage = open_sqlite_storage(":memory:")
    config = push_config()
    calls: list[str] = []
    gate = {"fail": False}

    async def hook(key):
        calls.append(key)
        if gate["fail"]:
            raise RuntimeError(f"boom {key}")

    app = FastAPI()
    app.include_router(push_router(storage, Authenticator(storage), config, on_discarded=hook))
    app.state.storage = storage
    with TestClient(app) as c:
        from wristcall.users import UserService

        users = UserService(storage)
        user = run(users.create("owner"))
        token = run(users.issue_token(user.id, "t"))[1]
        yield c, token, calls, gate


def test_replaced_key_is_discarded(hooked):
    c, token, calls, _ = hooked
    assert put(c, token, KEY_A).status_code == 204
    assert calls == []  # nothing replaced
    assert put(c, token, KEY_A).status_code == 204  # same key: idempotent
    assert calls == []
    assert put(c, token, KEY_B).status_code == 204
    settle(calls, 1)
    assert calls == [KEY_A]


def test_deleted_key_is_discarded(hooked):
    c, token, calls, _ = hooked
    put(c, token, KEY_A)
    assert c.delete("/v1/push", headers=h(token)).status_code == 204
    settle(calls, 1)
    assert calls == [KEY_A]
    assert c.delete("/v1/push", headers=h(token)).status_code == 404
    settle(calls, 2)
    assert calls == [KEY_A]


def test_failing_hook_does_not_fail_the_request(hooked, caplog):
    c, token, calls, gate = hooked
    caplog.set_level(logging.DEBUG)
    put(c, token, KEY_A)
    gate["fail"] = True
    assert put(c, token, KEY_B).status_code == 204
    settle(calls, 1)
    assert calls == [KEY_A]
    assert "discarding a push key failed: RuntimeError" in caplog.text
    assert KEY_A not in caplog.text


def test_hook_does_not_block_the_response():
    storage = open_sqlite_storage(":memory:")
    async def scenario():
        from wristcall.users import UserService

        users = UserService(storage)
        user = await users.create("owner")
        token = (await users.issue_token(user.id, "t"))[1]
        gate = asyncio.Event()
        done: list[str] = []

        async def hook(key):
            await gate.wait()
            done.append(key)

        app = FastAPI()
        app.include_router(push_router(storage, Authenticator(storage), push_config(), on_discarded=hook))
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await c.put("/v1/push", headers=h(token), json={"push_key": KEY_A})
            r = await asyncio.wait_for(c.put("/v1/push", headers=h(token), json={"push_key": KEY_B}), 2)
            assert r.status_code == 204 and done == []  # response came back while the hook is still waiting
            gate.set()
            await asyncio.sleep(0.05)
            assert done == [KEY_A]

    run(scenario())
