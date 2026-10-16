"""Silent accounts, device enrollment (account key or a fresh Zitadel token) and rotating sessions."""

import base64
import logging
import os
import re
import time
from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pymongo import MongoClient

from conftest import CLIENTS, ISSUER, mongo_url, serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.limits import AccountCreationLimiter, AttemptLimiter, PollLimiter, limit_key
from wristcall_cloud.signing import SigningKey
from wristcall_cloud.store import Store

PUBLIC_URL = "https://cloud.test"
KEY_A = f"{ISSUER}#a"
ACC_ID = re.compile(r"^acc_[a-z2-7]{26}$")
SESSION_FIELDS = {"account", "device", "access_token", "refresh_token", "expires_at"}


def new_key_b64() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def device(platform: str = "ios", name: str = "phone") -> dict[str, str]:
    return {"name": name, "platform": platform}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_signing_key() -> SigningKey:
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return SigningKey(pem)


@pytest.fixture
def signing_key() -> SigningKey:
    return new_signing_key()


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(
        mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), public_url=PUBLIC_URL, legacy_zitadel_tokens=True
    )


@pytest.fixture
def app(config, mongo_db, fake_verifier, signing_key):
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)


def served(config, mongo_db, fake_verifier, signing_key):
    """A TestClient for an app built from `config` (use as `for c in served(...)`)."""
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)
    yield from serve(app, mongo_db)


def create_account(client, key: str | None = None, platform: str = "ios", headers=None) -> dict[str, Any]:
    r = client.post(
        "/v1/accounts", json={"account_key": key or new_key_b64(), "device": device(platform)}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def enroll_by_key(client, key: str, platform: str = "ios", **extra) -> Any:
    return client.post("/v1/devices", json={"account_key": key, "device": device(platform), **extra})


def insert_legacy_account(mongo_db, with_server: bool = True) -> None:
    """An account (and a server) written by 0.3.x, before this Cloud started."""
    now = time.time()
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        db = sync[mongo_db.name]
        db["accounts"].insert_one({"_id": KEY_A, "created_at": now, "last_seen_at": now})
        if with_server:
            db["servers"].insert_one(
                {
                    "_id": "srv_old",
                    "account": KEY_A,
                    "name": "home",
                    "url": "https://home.test",
                    "kind": "self-hosted",
                    "linked": True,
                    "agents": [],
                    "created_at": now,
                    "updated_at": now,
                }
            )


# Limits


@pytest.mark.parametrize(
    ("ip", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8::1", "2001:db8::/64"),
        ("::ffff:1.2.3.4", "1.2.3.4"),
        ("unknown", "unknown"),
        ("testclient", "testclient"),
    ],
)
def test_limit_key(ip, key):
    assert limit_key(ip) == key


def test_limiters_windows():
    clock = [1000.0]
    accounts = AccountCreationLimiter(per_hour=2, per_day=3, now=lambda: clock[0])
    assert accounts.allow("ip") is None and accounts.allow("ip") is None
    assert accounts.allow("ip") == pytest.approx(3600)
    assert accounts.allow("other") is None
    clock[0] += 3600
    assert accounts.allow("ip") is None
    assert accounts.allow("ip") == pytest.approx(86400 - 3600)

    attempts = AttemptLimiter(per_minute=1, now=lambda: clock[0])
    assert attempts.allow("ip") is None
    assert attempts.allow("ip") == pytest.approx(60)

    polls = PollLimiter(now=lambda: clock[0])
    assert polls.allow("session") is None
    assert polls.allow("session") == pytest.approx(1)
    clock[0] += 1
    assert polls.allow("session") is None


def test_app_has_the_limiters(app):
    assert isinstance(app.state.account_limiter, AccountCreationLimiter)
    assert isinstance(app.state.attempt_limiter, AttemptLimiter)
    assert isinstance(app.state.poll_limiter, PollLimiter)


# POST /v1/accounts


def test_create_account_returns_a_working_session(client):
    session = create_account(client)
    assert set(session) == SESSION_FIELDS
    assert ACC_ID.match(session["account"])
    assert session["device"].startswith("dev_")
    assert session["refresh_token"].startswith("wcr_")
    assert session["expires_at"] > time.time()
    r = client.get("/v1/account", headers=bearer(session["access_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["account"] == session["account"] and r.json()["key_set"] is True


def test_create_account_twice_with_the_same_key_is_409(client):
    key = new_key_b64()
    create_account(client, key)
    r = client.post("/v1/accounts", json={"account_key": key, "device": device()})
    assert r.status_code == 409
    assert r.json()["error"] == "key_in_use"


@pytest.mark.parametrize(
    "body",
    [
        {"account_key": "short", "device": device()},
        {"account_key": new_key_b64(), "device": device("android")},
        {"account_key": new_key_b64(), "device": device(name="")},
        {"account_key": new_key_b64(), "device": device(name="x" * 65)},
    ],
)
def test_create_account_bad_key_or_device_is_422(client, body):
    r = client.post("/v1/accounts", json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid"


def test_create_account_per_ip_limit(config, mongo_db, fake_verifier, signing_key):
    proxied = replace(config, client_ip_header="X-Forwarded-For")
    for c in served(proxied, mongo_db, fake_verifier, signing_key):
        same = {"X-Forwarded-For": "198.51.100.1"}
        for _ in range(3):
            create_account(c, headers=same)
        r = c.post("/v1/accounts", json={"account_key": new_key_b64(), "device": device()}, headers=same)
        assert r.status_code == 429
        assert r.json()["error"] == "rate_limited"
        assert 3500 < int(r.headers["Retry-After"]) <= 3600
        create_account(c, headers={"X-Forwarded-For": "198.51.100.2"})


def test_create_account_global_cap(config, mongo_db, fake_verifier, signing_key, caplog):
    capped = replace(config, accounts_per_day=2, accounts_alert_per_day=1, client_ip_header="X-Forwarded-For")
    for c in served(capped, mongo_db, fake_verifier, signing_key):
        with caplog.at_level(logging.WARNING):
            create_account(c, headers={"X-Forwarded-For": "198.51.100.1"})
            create_account(c, headers={"X-Forwarded-For": "198.51.100.2"})
            key = new_key_b64()
            r = c.post(
                "/v1/accounts",
                json={"account_key": key, "device": device()},
                headers={"X-Forwarded-For": "198.51.100.3"},
            )
        assert r.status_code == 429
        assert r.json()["error"] == "rate_limited"
        assert r.headers["Retry-After"] == "3600"
        assert "account creation cap reached" in caplog.text
        assert "accounts created in 24 h: 2" in caplog.text
        assert "198.51.100" not in caplog.text and key not in caplog.text


def test_no_signing_key_means_not_configured(config, mongo_db, fake_verifier):
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier)
    for c in serve(app, mongo_db):
        for method, path in [
            ("POST", "/v1/accounts"),
            ("POST", "/v1/devices"),
            ("POST", "/v1/session/refresh"),
            ("GET", "/v1/devices"),
            ("DELETE", "/v1/devices/dev_x"),
            ("PUT", "/v1/account/key"),
            ("POST", "/v1/account/key/rotate"),
        ]:
            r = c.request(method, path, json={"account_key": new_key_b64(), "device": device()})
            assert r.status_code == 404, (method, path)
            assert r.json()["error"] == "not_configured", (method, path)


# POST /v1/devices by account key


def test_devices_by_key_without_account_id(client):
    key = new_key_b64()
    first = create_account(client, key)
    r = enroll_by_key(client, key, "ipados")
    assert r.status_code == 201, r.text
    session = r.json()
    assert set(session) == SESSION_FIELDS | {"key_set"}
    assert session["account"] == first["account"] and session["key_set"] is True
    assert session["device"] != first["device"]
    assert client.get("/v1/account", headers=bearer(session["access_token"])).json()["account"] == first["account"]


def test_devices_by_key_with_wrong_account_is_401(client):
    key = new_key_b64()
    mine = create_account(client, key)
    other = create_account(client)
    r = enroll_by_key(client, key, account=other["account"])
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized", "message": "unknown account key"}
    assert enroll_by_key(client, key, account=mine["account"]).status_code == 201


def test_devices_unknown_key_is_401(client):
    r = enroll_by_key(client, new_key_b64())
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized", "message": "unknown account key"}


def test_devices_bad_key_is_422(client):
    r = enroll_by_key(client, "not-a-key")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid"


def test_devices_attempts_are_limited_per_ip(config, mongo_db, fake_verifier, signing_key):
    strict = replace(config, session_attempts_per_minute=2)
    for c in served(strict, mongo_db, fake_verifier, signing_key):
        assert enroll_by_key(c, new_key_b64()).status_code == 401
        assert enroll_by_key(c, new_key_b64()).status_code == 401
        r = enroll_by_key(c, new_key_b64())
        assert r.status_code == 429
        assert r.json()["error"] == "rate_limited"
        assert 0 < int(r.headers["Retry-After"]) <= 60


# POST /v1/devices with a Zitadel token


def test_devices_with_fresh_zitadel_token_maps_the_migrated_account(mongo_db, config, fake_verifier, signing_key):
    insert_legacy_account(mongo_db)
    fake_verifier.add("fresh", "a", client_id="client-ios", issued_at=time.time() - 60)
    for c in served(config, mongo_db, fake_verifier, signing_key):
        r = c.post("/v1/devices", json={"device": device()}, headers=bearer("fresh"))
        assert r.status_code == 201, r.text
        session = r.json()
        assert set(session) == SESSION_FIELDS | {"key_set"}
        assert session["account"] == KEY_A and session["key_set"] is False
        servers = c.get("/v1/servers", headers=bearer(session["access_token"])).json()["servers"]
        assert [s["id"] for s in servers] == ["srv_old"]


def test_devices_with_old_zitadel_token_is_stale(client, fake_verifier):
    fake_verifier.add("old", "a", client_id="client-ios", issued_at=time.time() - 601)
    r = client.post("/v1/devices", json={"device": device()}, headers=bearer("old"))
    assert r.status_code == 401
    assert r.json() == {
        "error": "stale_token",
        "message": "sign in again: the sign-in token is older than 600 seconds",
    }


def test_devices_with_zitadel_token_of_a_new_user_creates_acc_account(client, fake_verifier):
    fake_verifier.add("new-user", "b", client_id="client-pwa")
    r = client.post("/v1/devices", json={"device": device("web")}, headers=bearer("new-user"))
    assert r.status_code == 201, r.text
    session = r.json()
    assert ACC_ID.match(session["account"]) and session["key_set"] is False
    # The same identity lands on the same account next time.
    again = client.post("/v1/devices", json={"device": device("web")}, headers=bearer("new-user")).json()
    assert again["account"] == session["account"]


@pytest.mark.parametrize(
    ("client_id", "platform", "status"),
    [
        ("client-watch", "ios", 403),
        ("client-watch", "web", 403),
        ("client-watch", "watchos", 201),
        ("client-ios", "watchos", 403),
        ("client-ios", "macos", 201),
        ("client-pwa", "ios", 403),
        ("client-pwa", "web", 201),
    ],
)
def test_devices_zitadel_client_must_match_platform(client, fake_verifier, client_id, platform, status):
    fake_verifier.add("tok", "a", client_id=client_id)
    r = client.post("/v1/devices", json={"device": device(platform)}, headers=bearer("tok"))
    assert r.status_code == status, r.text
    if status == 403:
        assert r.json()["error"] == "forbidden"


def test_devices_both_modes_is_422(client, fake_verifier):
    fake_verifier.add("tok", "a")
    key = new_key_b64()
    create_account(client, key)
    r = client.post("/v1/devices", json={"account_key": key, "device": device()}, headers=bearer("tok"))
    assert r.status_code == 422
    assert r.json() == {"error": "invalid", "message": "send either an account key or a sign-in token"}


def test_devices_bad_zitadel_token_is_401(client):
    r = client.post("/v1/devices", json={"device": device()}, headers=bearer("nope"))
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_devices_zitadel_down_is_503(client, fake_verifier):
    fake_verifier.add("tok", "a")
    fake_verifier.down = True
    r = client.post("/v1/devices", json={"device": device()}, headers=bearer("tok"))
    assert r.status_code == 503
    assert r.json()["error"] == "account_unavailable"


def test_devices_account_token_is_not_a_sign_in_token(client, fake_verifier):
    session = create_account(client)
    calls: list[str] = []
    verify = fake_verifier.verify

    async def spy(token: str):
        calls.append(token)
        return await verify(token)

    fake_verifier.verify = spy
    r = client.post("/v1/devices", json={"device": device()}, headers=bearer(session["access_token"]))
    assert r.status_code == 401
    assert calls == []


def test_device_limit(config, mongo_db, fake_verifier, signing_key):
    for c in served(replace(config, max_devices=2), mongo_db, fake_verifier, signing_key):
        key = new_key_b64()
        create_account(c, key)
        assert enroll_by_key(c, key).status_code == 201
        r = enroll_by_key(c, key)
        assert r.status_code == 403
        assert r.json()["error"] == "limit"


# POST /v1/session/refresh


def test_refresh_rotates(client):
    session = create_account(client)
    r = client.post("/v1/session/refresh", json={"refresh_token": session["refresh_token"], "assertion": "x"})
    assert r.status_code == 200, r.text
    renewed = r.json()
    assert set(renewed) == SESSION_FIELDS
    assert renewed["account"] == session["account"] and renewed["device"] == session["device"]
    assert renewed["refresh_token"] != session["refresh_token"]
    assert client.get("/v1/devices", headers=bearer(renewed["access_token"])).status_code == 200
    again = client.post("/v1/session/refresh", json={"refresh_token": renewed["refresh_token"]})
    assert again.status_code == 200


def test_refresh_reuse_revokes_the_device(client, caplog):
    session = create_account(client)
    renewed = client.post("/v1/session/refresh", json={"refresh_token": session["refresh_token"]}).json()
    with caplog.at_level(logging.WARNING):
        r = client.post("/v1/session/refresh", json={"refresh_token": session["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["error"] == "revoked"
    assert "refresh token reused; device revoked" in caplog.text
    assert session["device"] not in caplog.text
    assert client.get("/v1/devices", headers=bearer(renewed["access_token"])).status_code == 401
    r = client.post("/v1/session/refresh", json={"refresh_token": renewed["refresh_token"]})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"refresh_token": None},
        {"refresh_token": 12},
        {"refresh_token": "abc"},
        {"refresh_token": "wcr_" + "x" * 125},
        {"refresh_token": "wcr_unknown"},
    ],
)
def test_refresh_garbage_is_401(client, body):
    r = client.post("/v1/session/refresh", json=body)
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_refresh_idle_device_is_401(client, mongo_db):
    session = create_account(client)
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        sync[mongo_db.name]["devices"].update_one(
            {"_id": session["device"]}, {"$set": {"last_seen_at": time.time() - 91 * 86400}}
        )
    r = client.post("/v1/session/refresh", json={"refresh_token": session["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


# GET and DELETE /v1/devices


def test_list_and_revoke_devices(client):
    key = new_key_b64()
    phone = create_account(client, key)
    pad = enroll_by_key(client, key, "ipados").json()
    mac = enroll_by_key(client, key, "macos").json()
    r = client.get("/v1/devices", headers=bearer(phone["access_token"]))
    assert r.status_code == 200, r.text
    devices = r.json()["devices"]
    assert [d["id"] for d in devices] == [phone["device"], pad["device"], mac["device"]]
    assert [d["current"] for d in devices] == [True, False, False]
    assert set(devices[0]) == {"id", "name", "platform", "created_at", "last_seen_at", "current"}
    assert devices[1]["platform"] == "ipados" and devices[0]["name"] == "phone"

    assert client.delete(f"/v1/devices/{pad['device']}", headers=bearer(phone["access_token"])).status_code == 204
    assert client.get("/v1/devices", headers=bearer(pad["access_token"])).status_code == 401
    r = client.delete(f"/v1/devices/{pad['device']}", headers=bearer(phone["access_token"]))
    assert r.status_code == 404 and r.json()["error"] == "not_found"

    assert client.delete(f"/v1/devices/{mac['device']}", headers=bearer(mac["access_token"])).status_code == 204
    assert client.get("/v1/devices", headers=bearer(mac["access_token"])).status_code == 401
    left = client.get("/v1/devices", headers=bearer(phone["access_token"])).json()["devices"]
    assert [d["id"] for d in left] == [phone["device"]]


def test_devices_of_another_account_are_not_reachable(client):
    mine = create_account(client)
    other = create_account(client)
    r = client.delete(f"/v1/devices/{other['device']}", headers=bearer(mine["access_token"]))
    assert r.status_code == 404
    assert client.get("/v1/devices", headers=bearer(other["access_token"])).status_code == 200


def test_watch_cannot_manage_the_account(client):
    key = new_key_b64()
    phone = create_account(client, key)
    watch = enroll_by_key(client, key, "watchos").json()
    headers = bearer(watch["access_token"])
    assert client.put("/v1/account/key", json={"account_key": key}, headers=headers).status_code == 403
    r = client.post(
        "/v1/account/key/rotate", json={"account_key": key, "new_account_key": new_key_b64()}, headers=headers
    )
    assert r.status_code == 403
    assert client.delete(f"/v1/devices/{phone['device']}", headers=headers).status_code == 403
    assert client.delete("/v1/account", headers=headers).status_code == 403
    assert client.get("/v1/devices", headers=headers).status_code == 200
    r = client.post("/v1/server-tokens", json={"audience": "https://home.test"}, headers=headers)
    assert r.status_code == 200, r.text
    # The watch may still sign itself out.
    assert client.delete(f"/v1/devices/{watch['device']}", headers=headers).status_code == 204


def test_web_device_may_revoke_others(client, fake_verifier):
    fake_verifier.add("pwa", "b", client_id="client-pwa")
    web = client.post("/v1/devices", json={"device": device("web")}, headers=bearer("pwa")).json()
    other = client.post("/v1/devices", json={"device": device("web")}, headers=bearer("pwa")).json()
    r = client.delete(f"/v1/devices/{other['device']}", headers=bearer(web["access_token"]))
    assert r.status_code == 204


def test_zitadel_token_cannot_use_new_routes(client, fake_verifier):
    fake_verifier.add("tok", "a")
    for method, path in [
        ("GET", "/v1/devices"),
        ("DELETE", "/v1/devices/dev_x"),
        ("PUT", "/v1/account/key"),
        ("POST", "/v1/account/key/rotate"),
    ]:
        r = client.request(method, path, json={}, headers=bearer("tok"))
        assert r.status_code == 401, (method, path)


# PUT /v1/account/key and POST /v1/account/key/rotate


def test_put_key_sets_once(client, fake_verifier):
    fake_verifier.add("tok", "a")
    session = client.post("/v1/devices", json={"device": device()}, headers=bearer("tok")).json()
    assert session["key_set"] is False
    headers = bearer(session["access_token"])
    key = new_key_b64()
    assert client.put("/v1/account/key", json={"account_key": key}, headers=headers).status_code == 204
    assert client.get("/v1/account", headers=headers).json()["key_set"] is True
    assert client.put("/v1/account/key", json={"account_key": key}, headers=headers).status_code == 204
    r = client.put("/v1/account/key", json={"account_key": new_key_b64()}, headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "key_exists"
    # The key now enrolls devices into that account.
    assert enroll_by_key(client, key).json()["account"] == session["account"]

    fake_verifier.add("tok-c", "c")
    other = client.post("/v1/devices", json={"device": device()}, headers=bearer("tok-c")).json()
    r = client.put("/v1/account/key", json={"account_key": key}, headers=bearer(other["access_token"]))
    assert r.status_code == 409 and r.json()["error"] == "key_in_use"
    r = client.put("/v1/account/key", json={"account_key": "short"}, headers=bearer(other["access_token"]))
    assert r.status_code == 422


def test_rotate_key_signs_out_everywhere(client):
    key = new_key_b64()
    phone = create_account(client, key)
    pad = enroll_by_key(client, key, "ipados").json()
    watch = enroll_by_key(client, key, "watchos").json()
    new_key = new_key_b64()
    r = client.post(
        "/v1/account/key/rotate",
        json={"account_key": key, "new_account_key": new_key},
        headers=bearer(phone["access_token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"revoked": 2}
    assert client.get("/v1/devices", headers=bearer(pad["access_token"])).status_code == 401
    assert client.get("/v1/devices", headers=bearer(watch["access_token"])).status_code == 401
    assert client.get("/v1/devices", headers=bearer(phone["access_token"])).status_code == 200
    assert enroll_by_key(client, key).status_code == 401
    r = enroll_by_key(client, new_key)
    assert r.status_code == 201 and r.json()["account"] == phone["account"]


def test_rotate_needs_the_current_key(client, fake_verifier):
    key = new_key_b64()
    phone = create_account(client, key)
    pad = enroll_by_key(client, key, "ipados").json()
    r = client.post(
        "/v1/account/key/rotate",
        json={"account_key": new_key_b64(), "new_account_key": new_key_b64()},
        headers=bearer(phone["access_token"]),
    )
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert client.get("/v1/devices", headers=bearer(pad["access_token"])).status_code == 200
    assert client.get("/v1/devices", headers=bearer(phone["access_token"])).status_code == 200
    assert enroll_by_key(client, key).status_code == 201

    fake_verifier.add("tok", "a")
    keyless = client.post("/v1/devices", json={"device": device()}, headers=bearer("tok")).json()
    r = client.post(
        "/v1/account/key/rotate",
        json={"account_key": new_key_b64(), "new_account_key": new_key_b64()},
        headers=bearer(keyless["access_token"]),
    )
    assert r.status_code == 409 and r.json()["error"] == "no_key"


def test_rotate_to_a_key_in_use_is_409(client):
    key = new_key_b64()
    phone = create_account(client, key)
    taken = new_key_b64()
    create_account(client, taken)
    r = client.post(
        "/v1/account/key/rotate",
        json={"account_key": key, "new_account_key": taken},
        headers=bearer(phone["access_token"]),
    )
    assert r.status_code == 409 and r.json()["error"] == "key_in_use"
    r = client.post(
        "/v1/account/key/rotate",
        json={"account_key": key, "new_account_key": "short"},
        headers=bearer(phone["access_token"]),
    )
    assert r.status_code == 422


def test_rotate_attempts_are_limited(config, mongo_db, fake_verifier, signing_key):
    for c in served(replace(config, session_attempts_per_minute=2), mongo_db, fake_verifier, signing_key):
        phone = create_account(c)
        body = {"account_key": new_key_b64(), "new_account_key": new_key_b64()}
        assert c.post("/v1/account/key/rotate", json=body, headers=bearer(phone["access_token"])).status_code == 401
        assert c.post("/v1/account/key/rotate", json=body, headers=bearer(phone["access_token"])).status_code == 401
        r = c.post("/v1/account/key/rotate", json=body, headers=bearer(phone["access_token"]))
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"


def test_ipv6_limit_by_prefix(config, mongo_db, fake_verifier, signing_key):
    proxied = replace(config, client_ip_header="X-Forwarded-For")
    for c in served(proxied, mongo_db, fake_verifier, signing_key):
        for n in range(1, 4):
            create_account(c, headers={"X-Forwarded-For": f"2001:db8::{n}"})
        r = c.post(
            "/v1/accounts",
            json={"account_key": new_key_b64(), "device": device()},
            headers={"X-Forwarded-For": "2001:db8::4"},
        )
        assert r.status_code == 429
        create_account(c, headers={"X-Forwarded-For": "2001:db9::1"})


# Secrets


def test_secrets_never_reach_logs(client, fake_verifier, caplog):
    secrets: list[str] = []
    with caplog.at_level(logging.DEBUG):
        key = new_key_b64()
        phone = create_account(client, key)
        renewed = client.post("/v1/session/refresh", json={"refresh_token": phone["refresh_token"]}).json()
        reused = client.post("/v1/session/refresh", json={"refresh_token": phone["refresh_token"]})
        assert reused.status_code == 401
        pad = enroll_by_key(client, key, "ipados").json()
        new_key = new_key_b64()
        client.post(
            "/v1/account/key/rotate",
            json={"account_key": key, "new_account_key": new_key},
            headers=bearer(pad["access_token"]),
        )
        client.post(
            "/v1/account/key/rotate",
            json={"account_key": key, "new_account_key": new_key_b64()},
            headers=bearer(pad["access_token"]),
        )
        enroll_by_key(client, key)
        fake_verifier.add("zitadel-secret-token", "z", issued_at=time.time() - 601)
        client.post("/v1/devices", json={"device": device()}, headers=bearer("zitadel-secret-token"))
        secrets += [key, new_key, "zitadel-secret-token"]
        for session in (phone, renewed, pad):
            secrets += [session["access_token"], session["refresh_token"]]
    assert caplog.records, "expected some logging to check"
    for secret in secrets:
        assert secret not in caplog.text
