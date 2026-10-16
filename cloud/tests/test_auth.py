"""Authentication: account session tokens on every route, Zitadel tokens only behind the transition flag."""

import functools
import logging
import secrets
import time
from dataclasses import replace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends
from pymongo import AsyncMongoClient, MongoClient

from conftest import CLIENTS, ISSUER, mongo_url, serve
from wristcall_cloud.api_common import ApiError, parse_device
from wristcall_cloud.app import create_app
from wristcall_cloud.auth import (
    Caller,
    client_platform,
    current_account,
    current_device,
    platform_client,
    require_platform,
)
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.identity import IdentityUnavailable
from wristcall_cloud.session_tokens import MANAGER_PLATFORMS, AccountTokens, secret_hash
from wristcall_cloud.signing import SigningKey
from wristcall_cloud.store import Store

PUBLIC_URL = "https://cloud.test"
KEY_A = f"{ISSUER}#a"
H_ZITADEL = {"Authorization": "Bearer token-a"}


def new_key() -> SigningKey:
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return SigningKey(pem)


class FakeUsers:
    """The identity provider's user deletion: records subjects; `answer` is what delete returns or raises."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.answer: bool | Exception = True

    async def delete(self, user_id: str) -> bool:
        if isinstance(self.answer, Exception):
            raise self.answer
        self.deleted.append(user_id)
        return self.answer


@pytest.fixture(autouse=True)
def zitadel_tokens(fake_verifier):
    fake_verifier.add("token-a", "a", client_id="client-ios")
    fake_verifier.add("token-watch", "a", client_id="client-watch")


@pytest.fixture
def signing_key() -> SigningKey:
    return new_key()


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(
        mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), public_url=PUBLIC_URL, legacy_zitadel_tokens=True
    )


@pytest.fixture
def users() -> FakeUsers:
    return FakeUsers()


@pytest.fixture
def app(config, mongo_db, fake_verifier, signing_key, users):
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key, users=users)


def sync_db(_mongo_db=None):
    return MongoClient(mongo_url(), serverSelectionTimeoutMS=5000)


def fresh(mongo_db):
    """The same database through a new async client: each TestClient runs a loop of its own."""
    return AsyncMongoClient(mongo_url(), serverSelectionTimeoutMS=5000, tz_aware=True)[mongo_db.name]


def run(client, fn, *args, **kwargs) -> Any:
    """Runs a coroutine function on the app's loop (the async Mongo client is bound to it)."""
    return client.portal.call(functools.partial(fn, *args, **kwargs))


def new_account(client) -> str:
    accounts = client.app.state.accounts
    return run(client, accounts.create_account, secret_hash(secrets.token_hex(16)))["_id"]


def new_device(client, platform: str = "ios", account: str | None = None) -> tuple[str, str, dict[str, str]]:
    """An account (new unless given), a device of it and that device's access token as a header."""
    accounts = client.app.state.accounts
    account = account or new_account(client)
    doc, _ = run(client, accounts.add_device, account, {"name": "phone", "platform": platform}, max_devices=10)
    token, _ = client.app.state.account_tokens.issue(account, doc["_id"], platform)
    return account, doc["_id"], {"Authorization": f"Bearer {token}"}


def add_server(client, headers, url="https://home.test") -> dict[str, Any]:
    r = client.post("/v1/servers", json={"name": "home", "url": url}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# Account tokens


def test_account_token_reaches_the_agenda(client):
    _, _, headers = new_device(client)
    assert client.get("/v1/servers", headers=headers).json() == {"servers": []}
    server = add_server(client, headers)
    assert client.get("/v1/servers", headers=headers).json()["servers"] == [server]
    assert client.get("/v1/agents", headers=headers).status_code == 200


def test_account_token_records_the_device_was_seen(client, mongo_db):
    account, device, headers = new_device(client)
    with sync_db(mongo_db) as sync:
        db = sync[mongo_db.name]
        db["devices"].update_one({"_id": device}, {"$set": {"last_seen_at": 0}})
        db["accounts"].update_one({"_id": account}, {"$set": {"last_seen_at": 0}})
    assert client.get("/v1/servers", headers=headers).status_code == 200
    with sync_db(mongo_db) as sync:
        db = sync[mongo_db.name]
        assert db["devices"].find_one({"_id": device})["last_seen_at"] > 0
        assert db["accounts"].find_one({"_id": account})["last_seen_at"] > 0


def test_revoked_device_access_token_is_401(client):
    account, device, headers = new_device(client)
    assert client.get("/v1/servers", headers=headers).status_code == 200
    assert run(client, client.app.state.accounts.delete_device, account, device) is True
    r = client.get("/v1/servers", headers=headers)
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_account_token_of_another_cloud_is_401(client, fake_verifier):
    account, device, _ = new_device(client)
    forged, _ = AccountTokens(new_key(), PUBLIC_URL).issue(account, device, "ios")
    # Even a verifier that would take it is never asked: an account token is never a Zitadel token.
    fake_verifier.add(forged, "a")
    r = client.get("/v1/servers", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert forged not in r.text


def test_account_token_for_another_account_is_401(client):
    other = new_account(client)
    _, device, _ = new_device(client)
    token, _ = client.app.state.account_tokens.issue(other, device, "ios")
    assert client.get("/v1/servers", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_expired_account_token_is_401(client):
    account, device, _ = new_device(client)
    tokens = AccountTokens(client.app.state.signing_key, PUBLIC_URL, now=lambda: time.time() - 7200)
    token, _ = tokens.issue(account, device, "ios")
    assert client.get("/v1/servers", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_zitadel_token_without_legacy_flag_is_401(config, mongo_db, fake_verifier, signing_key):
    off = replace(config, legacy_zitadel_tokens=False)
    app = create_app(off, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)
    for c in serve(app, mongo_db):
        for method, path in [("GET", "/v1/account"), ("GET", "/v1/servers"), ("GET", "/v1/agents")]:
            r = c.request(method, path, headers=H_ZITADEL)
            assert r.status_code == 401, path
            assert r.json() == {"error": "unauthorized", "message": "sign in with an account session"}
        r = c.post("/v1/server-tokens", json={"audience": "https://home.test"}, headers=H_ZITADEL)
        assert r.status_code == 401
        # The account token still works there.
        _, _, headers = new_device(c)
        assert c.get("/v1/servers", headers=headers).status_code == 200


# Zitadel tokens behind the transition flag (0.3.x clients)


def test_legacy_flag_keeps_old_routes_working(client, fake_verifier, signing_key):
    # The PWA 0.1.0 keeps a Zitadel token for up to 12 h: one issued 11 h ago still works on the 0.3.x routes.
    fake_verifier.add("token-old", "a", client_id="client-pwa", issued_at=time.time() - 39600)
    h = {"Authorization": "Bearer token-old"}
    r = client.get("/v1/account", headers=h)
    assert r.status_code == 200
    assert r.json()["account"] == KEY_A
    assert r.json()["servers"] == 0
    r = client.post("/v1/servers", json={"name": "home", "url": "https://home.test"}, headers=h)
    assert r.status_code == 201
    server = r.json()
    assert client.get("/v1/servers", headers=h).json() == {"servers": [server]}
    agents = [{"id": "ag1", "slug": "ag", "display_name": "Ag", "icon": "waveform", "call_type": "conversation"}]
    r = client.put(f"/v1/servers/{server['id']}/agents", json={"agents": agents}, headers=h)
    assert r.status_code == 200
    assert [a["id"] for a in client.get("/v1/agents", headers=h).json()["agents"]] == ["ag1"]
    r = client.post("/v1/server-tokens", json={"audience": "https://home.test"}, headers=h)
    assert r.status_code == 200
    assert jwt.decode(r.json()["token"], options={"verify_signature": False})["sub"] == KEY_A
    assert client.get("/v1/account", headers=h).json()["servers"] == 1


def test_legacy_server_token_keeps_the_account_id(client, signing_key):
    r = client.post("/v1/server-tokens", json={"audience": "https://home.test"}, headers=H_ZITADEL)
    assert r.status_code == 200
    claims = jwt.decode(
        r.json()["token"], signing_key.public_key, algorithms=["ES256"], audience="https://home.test", issuer=PUBLIC_URL
    )
    assert claims["sub"] == "https://auth.test#a"
    assert claims["client_id"] == "client-ios"


def test_legacy_zitadel_caller_gets_an_identity(client, mongo_db):
    assert client.get("/v1/servers", headers=H_ZITADEL).status_code == 200
    with sync_db(mongo_db) as sync:
        identity = sync[mongo_db.name]["identities"].find_one({"kind": "zitadel", "issuer": ISSUER, "subject": "a"})
    assert identity["account"] == KEY_A
    assert client.get("/v1/account", headers=H_ZITADEL).json()["identities"] == ["zitadel"]


def test_zitadel_issuer_down_is_503(client, fake_verifier):
    fake_verifier.down = True
    r = client.get("/v1/servers", headers=H_ZITADEL)
    assert r.status_code == 503
    assert r.json()["error"] == "account_unavailable"


def test_migrated_account_keeps_its_agenda(mongo_db, config, fake_verifier, signing_key):
    # An account and a server written by 0.3.x, before this Cloud ever started.
    now = time.time()
    with sync_db(mongo_db) as sync:
        db = sync[mongo_db.name]
        db["accounts"].insert_one({"_id": KEY_A, "created_at": now, "last_seen_at": now})
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
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)
    for c in serve(app, mongo_db):
        with sync_db(mongo_db) as sync:
            identity = sync[mongo_db.name]["identities"].find_one({"kind": "zitadel", "issuer": ISSUER, "subject": "a"})
        assert identity["account"] == KEY_A
        servers = c.get("/v1/servers", headers=H_ZITADEL).json()["servers"]
        assert [s["id"] for s in servers] == ["srv_old"]
        account = c.get("/v1/account", headers=H_ZITADEL).json()
        assert account["account"] == KEY_A and account["created_at"] == now and account["key_set"] is False


@pytest.mark.parametrize(
    ("platform", "client_id"),
    [("ios", "client-ios"), ("ipados", "client-ios"), ("watchos", "client-watch"), ("web", "client-pwa")],
)
def test_server_token_client_follows_the_device_platform(client, signing_key, platform, client_id):
    account, _, headers = new_device(client, platform)
    r = client.post("/v1/server-tokens", json={"audience": "https://home.test"}, headers=headers)
    assert r.status_code == 200
    claims = jwt.decode(
        r.json()["token"], signing_key.public_key, algorithms=["ES256"], audience="https://home.test", issuer=PUBLIC_URL
    )
    assert claims["sub"] == account
    assert claims["client_id"] == client_id


# GET /v1/account


def test_account_shows_plan_identities_and_key(client):
    account, _, headers = new_device(client)
    r = client.get("/v1/account", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["account"] == account
    assert isinstance(body["created_at"], float)
    assert (body["servers"], body["plan"], body["identities"], body["key_set"]) == (0, None, [], True)


def test_account_plan_is_the_unexpired_entitlement(client, mongo_db):
    account, _, headers = new_device(client)
    with sync_db(mongo_db) as sync:
        entitlements = sync[mongo_db.name]["entitlements"]
        entitlements.insert_one({"_id": account, "plan": "full", "source": "beta", "expires_at": None})
        assert client.get("/v1/account", headers=headers).json()["plan"] == {
            "plan": "full",
            "source": "beta",
            "expires_at": None,
        }
        later = time.time() + 3600
        entitlements.update_one({"_id": account}, {"$set": {"expires_at": later}})
        assert client.get("/v1/account", headers=headers).json()["plan"]["expires_at"] == later
        entitlements.update_one({"_id": account}, {"$set": {"expires_at": time.time() - 1}})
        assert client.get("/v1/account", headers=headers).json()["plan"] is None


# DELETE /v1/account


def test_delete_account_by_device_deletes_everything(client, mongo_db, users):
    account, _, headers = new_device(client)
    _, _, other = new_device(client, "watchos", account)
    accounts = client.app.state.accounts
    run(client, accounts.add_identity, account, "zitadel", ISSUER, "zsub")
    run(client, accounts.add_identity, account, "zitadel", "https://other-issuer.test", "elsewhere")
    add_server(client, headers)
    r = client.delete("/v1/account", headers=headers)
    assert r.status_code == 204
    assert users.deleted == ["zsub"]
    with sync_db(mongo_db) as sync:
        db = sync[mongo_db.name]
        assert db["servers"].count_documents({"account": account}) == 0
        assert db["devices"].count_documents({"account": account}) == 0
        assert db["identities"].count_documents({"account": account}) == 0
        assert db["accounts"].count_documents({"_id": account}) == 0
    for h in (headers, other):
        assert client.get("/v1/servers", headers=h).status_code == 401


def test_delete_account_zitadel_failure_keeps_devices(client, mongo_db, users):
    account, _, headers = new_device(client)
    run(client, client.app.state.accounts.add_identity, account, "zitadel", ISSUER, "zsub")
    add_server(client, headers)
    users.answer = IdentityUnavailable("user deletion answered 500")
    r = client.delete("/v1/account", headers=headers)
    assert r.status_code == 503
    assert r.json()["error"] == "account_unavailable"
    # The agenda is gone; the account, its devices and its identity are not: the same token retries.
    assert client.get("/v1/servers", headers=headers).json() == {"servers": []}
    with sync_db(mongo_db) as sync:
        doc = sync[mongo_db.name]["accounts"].find_one({"_id": account})
        assert "key_hash" in doc and doc["kind"] == "anonymous"
        assert sync[mongo_db.name]["identities"].count_documents({"account": account}) == 1
    users.answer = True
    assert client.delete("/v1/account", headers=headers).status_code == 204
    assert users.deleted == ["zsub"]
    with sync_db(mongo_db) as sync:
        assert sync[mongo_db.name]["accounts"].count_documents({"_id": account}) == 0


def test_delete_account_without_users_skips_zitadel(config, mongo_db, fake_verifier, signing_key):
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)
    for c in serve(app, mongo_db):
        account, _, headers = new_device(c, "web")
        run(c, c.app.state.accounts.add_identity, account, "zitadel", ISSUER, "zsub")
        assert c.delete("/v1/account", headers=headers).status_code == 204
        with sync_db(mongo_db) as sync:
            assert sync[mongo_db.name]["identities"].count_documents({"account": account}) == 0


def test_watch_device_cannot_delete_account(client, mongo_db, users):
    account, _, headers = new_device(client, "watchos")
    r = client.delete("/v1/account", headers=headers)
    assert r.status_code == 403
    assert r.json() == {"error": "forbidden", "message": "this device cannot do this"}
    assert client.get("/v1/servers", headers=headers).status_code == 200
    with sync_db(mongo_db) as sync:
        assert sync[mongo_db.name]["accounts"].count_documents({"_id": account}) == 1
    # A Zitadel token issued to the watch client: 403 as in 0.3.x.
    assert client.delete("/v1/account", headers={"Authorization": "Bearer token-watch"}).status_code == 403
    assert users.deleted == []


# /v1/config and start-up


def test_config_announces_sessions(client, config, mongo_db, fake_verifier):
    assert client.get("/v1/config").json()["sessions"] == {
        "enabled": True,
        "zitadel_max_age_s": 600,
        "legacy_zitadel_tokens": True,
    }
    db = fresh(mongo_db)
    off = replace(config, public_url=None, legacy_zitadel_tokens=False)
    plain = create_app(off, store=Store(db), verifier=fake_verifier)
    for c in serve(plain, db):
        assert c.get("/v1/config").json()["sessions"] == {
            "enabled": False,
            "zitadel_max_age_s": 600,
            "legacy_zitadel_tokens": False,
        }


def test_startup_logs_migration_and_legacy_flag(config, mongo_db, fake_verifier, signing_key, caplog):
    with sync_db(mongo_db) as sync:
        sync[mongo_db.name]["accounts"].insert_one({"_id": KEY_A, "created_at": 1.0, "last_seen_at": 1.0})
    caplog.set_level(logging.INFO, logger="wristcall_cloud.app")
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)
    for _ in serve(app, mongo_db):
        pass
    messages = [r.getMessage() for r in caplog.records if r.name == "wristcall_cloud.app"]
    assert "migration: 1 identities created" in messages
    legacy = "legacy Zitadel tokens accepted on account routes (WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS)"
    assert [r.levelno for r in caplog.records if r.getMessage() == legacy] == [logging.WARNING]
    caplog.clear()
    db = fresh(mongo_db)
    app = create_app(replace(config, legacy_zitadel_tokens=False), store=Store(db), verifier=fake_verifier)
    for _ in serve(app, db):
        pass
    messages = [r.getMessage() for r in caplog.records if r.name == "wristcall_cloud.app"]
    assert "migration: 0 identities created" in messages
    assert legacy not in messages


def test_client_ip_moved():
    from wristcall_cloud import api_common
    from wristcall_cloud.push import api

    assert api.client_ip is api_common.client_ip


# The dependencies and helpers


@pytest.fixture
def probe(app, client):
    @app.get("/_device")
    async def who(caller: Caller = Depends(current_device)) -> dict[str, Any]:
        return {"key": caller.key, "device": caller.device, "platform": caller.platform, "client_id": caller.client_id}

    @app.get("/_account")
    async def any_caller(caller: Caller = Depends(current_account)) -> dict[str, Any]:
        return {"key": caller.key, "device": caller.device, "platform": caller.platform, "client_id": caller.client_id}

    return client


def test_current_device_takes_only_account_tokens(probe):
    account, device, headers = new_device(probe, "web")
    assert probe.get("/_device", headers=headers).json() == {
        "key": account,
        "device": device,
        "platform": "web",
        "client_id": "client-pwa",
    }
    r = probe.get("/_device", headers=H_ZITADEL)
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert probe.get("/_device").status_code == 401


def test_zitadel_caller_platform_follows_its_client(probe):
    assert probe.get("/_account", headers=H_ZITADEL).json() == {
        "key": KEY_A,
        "device": None,
        "platform": "ios",
        "client_id": "client-ios",
    }
    assert probe.get("/_account", headers={"Authorization": "Bearer token-watch"}).json()["platform"] == "watchos"


def test_current_device_without_signing_key_is_404(config, mongo_db, fake_verifier):
    app = create_app(replace(config, public_url=None), store=Store(mongo_db), verifier=fake_verifier)

    @app.get("/_device")
    async def who(caller: Caller = Depends(current_device)) -> dict[str, Any]:
        return {"key": caller.key}

    for c in serve(app, mongo_db):
        r = c.get("/_device", headers=H_ZITADEL)
        assert r.status_code == 404
        assert r.json()["error"] == "not_configured"
        # The 0.3.x routes still take the Zitadel token (a Cloud without per-server tokens).
        assert c.get("/v1/servers", headers=H_ZITADEL).status_code == 200


def test_platform_client_and_back():
    assert [platform_client(p, CLIENTS) for p in ("ios", "ipados", "macos", "watchos", "web")] == [
        "client-ios",
        "client-ios",
        "client-ios",
        "client-watch",
        "client-pwa",
    ]
    assert platform_client("web", {"ios": "client-ios"}) is None
    assert client_platform("client-ios", CLIENTS) == "ios"
    assert client_platform("client-pwa", CLIENTS) == "web"
    assert client_platform("client-watch", CLIENTS) == "watchos"
    assert client_platform("someone-else", CLIENTS) == "watchos"
    assert client_platform(None, CLIENTS) == "watchos"
    assert client_platform(None, {"ios": "client-ios"}) == "watchos"


def test_require_platform():
    require_platform(Caller(key="k", client_id=None, platform="web", device="d"), MANAGER_PLATFORMS)
    with pytest.raises(ApiError) as e:
        require_platform(Caller(key="k", client_id=None, platform="watchos", device="d"), MANAGER_PLATFORMS)
    assert (e.value.code, e.value.message, e.value.status) == ("forbidden", "this device cannot do this", 403)


def test_parse_device():
    assert parse_device({"name": "Gustavo's iPhone", "platform": "ios", "attestation": "x", "extra": 1}) == {
        "name": "Gustavo's iPhone",
        "platform": "ios",
    }
    assert parse_device({"name": "w", "platform": "watchos", "public_key": "pk"}) == {
        "name": "w",
        "platform": "watchos",
        "public_key": "pk",
    }
    for bad in (
        None,
        [],
        {"platform": "ios"},
        {"name": "", "platform": "ios"},
        {"name": "x" * 65, "platform": "ios"},
        {"name": 1, "platform": "ios"},
        {"name": "n", "platform": "android"},
        {"name": "n"},
        {"name": "n", "platform": "ios", "public_key": "k" * 1025},
        {"name": "n", "platform": "ios", "public_key": 5},
    ):
        with pytest.raises(ApiError) as e:
            parse_device(bad)
        assert (e.value.code, e.value.status) == ("invalid", 422)


def test_api_error_retry_after_header(app, client):
    @app.get("/_slow")
    async def slow() -> None:
        raise ApiError("rate_limited", "too many accounts; try again later", 429, retry_after=30)

    r = client.get("/_slow")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "30"
    assert r.json() == {"error": "rate_limited", "message": "too many accounts; try again later"}
