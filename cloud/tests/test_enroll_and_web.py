"""Enroll codes (the iPhone adds a watch), web sessions approved by the iPhone and beta invites."""

import base64
import functools
import itertools
import os
import time
from dataclasses import replace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pymongo import MongoClient

from conftest import CLIENTS, ISSUER, mongo_url, serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.limits import PollLimiter
from wristcall_cloud.signing import SigningKey
from wristcall_cloud.store import Store

PUBLIC_URL = "https://cloud.test"
SESSION_FIELDS = {"account", "device", "access_token", "refresh_token", "expires_at"}


def new_key_b64() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def device(platform: str = "ios", name: str = "phone") -> dict[str, str]:
    return {"name": name, "platform": platform}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def signing_key() -> SigningKey:
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return SigningKey(pem)


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), public_url=PUBLIC_URL)


@pytest.fixture
def app(config, mongo_db, fake_verifier, signing_key):
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)


def served(config, mongo_db, fake_verifier, signing_key):
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)
    yield from serve(app, mongo_db)


def create_account(client, platform: str = "ios") -> dict[str, Any]:
    r = client.post("/v1/accounts", json={"account_key": new_key_b64(), "device": device(platform)})
    assert r.status_code == 201, r.text
    return r.json()


def enroll_code(client, phone: dict[str, Any]) -> str:
    r = client.post("/v1/devices/enroll-codes", headers=bearer(phone["access_token"]))
    assert r.status_code == 201, r.text
    return r.json()["code"]


def enroll(client, code: str, platform: str = "watchos"):
    return client.post("/v1/devices/enroll", json={"code": code, "device": device(platform, "watch")})


def web_session(client, headers=None) -> dict[str, Any]:
    r = client.post("/v1/web-sessions", json={"device": {"name": "Chrome on macOS"}}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def every_two_seconds(client) -> None:
    """The poll limiter sees each poll 2 s after the one before."""
    ticks = itertools.count(1000.0, 2.0)
    client.app.state.poll_limiter = PollLimiter(now=lambda: next(ticks))


# Enroll codes


def test_enroll_code_enrolls_the_watch(client):
    phone = create_account(client)
    r = client.post("/v1/devices/enroll-codes", headers=bearer(phone["access_token"]))
    assert r.status_code == 201
    body = r.json()
    assert body["code"].startswith("wce_") and body["expires_at"] == pytest.approx(time.time() + 600, abs=5)
    r = enroll(client, body["code"])
    assert r.status_code == 201, r.text
    watch = r.json()
    assert set(watch) == SESSION_FIELDS and watch["account"] == phone["account"]
    r = client.post("/v1/server-tokens", json={"audience": "https://home.test"}, headers=bearer(watch["access_token"]))
    assert r.status_code == 200, r.text
    claims = jwt.decode(r.json()["token"], options={"verify_signature": False})
    assert (claims["sub"], claims["client_id"]) == (phone["account"], "client-watch")
    devices = client.get("/v1/devices", headers=bearer(phone["access_token"])).json()["devices"]
    assert sorted(d["platform"] for d in devices) == ["ios", "watchos"]


def test_enroll_code_is_single_use(client):
    code = enroll_code(client, create_account(client))
    assert enroll(client, code).status_code == 201
    r = enroll(client, code)
    assert r.status_code == 404 and r.json()["error"] == "invalid_code"
    assert code not in r.text


def test_enroll_code_expired_is_401(client, mongo_db):
    # The contract answers an expired code like an unknown one: 404 invalid_code.
    code = enroll_code(client, create_account(client))
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        sync[mongo_db.name]["enroll_codes"].update_many({}, {"$set": {"expires_at": time.time() - 1}})
    r = enroll(client, code)
    assert r.status_code == 404 and r.json()["error"] == "invalid_code"


def test_enroll_code_cannot_create_an_app_device(client):
    phone = create_account(client)
    code = enroll_code(client, phone)
    for platform in ("ios", "ipados", "macos", "web"):
        r = enroll(client, code, platform)
        assert r.status_code == 403 and r.json()["error"] == "forbidden"
    # Not spent: the same code still enrolls the watch.
    assert enroll(client, code).status_code == 201


def test_watch_cannot_create_enroll_codes(client):
    phone = create_account(client)
    watch = enroll(client, enroll_code(client, phone)).json()
    r = client.post("/v1/devices/enroll-codes", headers=bearer(watch["access_token"]))
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_enroll_codes_open_limit(config, mongo_db, fake_verifier, signing_key):
    for c in served(replace(config, max_enroll_codes=2), mongo_db, fake_verifier, signing_key):
        phone = create_account(c)
        enroll_code(c, phone)
        enroll_code(c, phone)
        r = c.post("/v1/devices/enroll-codes", headers=bearer(phone["access_token"]))
        assert r.status_code == 403 and r.json()["error"] == "limit"


def test_enroll_device_limit_is_403(config, mongo_db, fake_verifier, signing_key):
    for c in served(replace(config, max_devices=1), mongo_db, fake_verifier, signing_key):
        code = enroll_code(c, create_account(c))
        r = enroll(c, code)
        assert r.status_code == 403 and r.json()["error"] == "limit"


def test_enroll_bad_body_is_422(client):
    assert client.post("/v1/devices/enroll", json={"device": device("watchos")}).status_code == 422
    assert client.post("/v1/devices/enroll", json={"code": "wce_x"}).status_code == 422
    r = client.post("/v1/devices/enroll", json={"code": "wce_nothing", "device": device("watchos")})
    assert r.status_code == 404 and r.json()["error"] == "invalid_code"


# Web sessions


def test_web_session_full_flow(client):
    every_two_seconds(client)
    phone = create_account(client)
    headers = bearer(phone["access_token"])
    r = client.post("/v1/web-sessions", json={"device": {"name": "Chrome on macOS", "platform": "web"}},
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
    assert r.status_code == 201, r.text
    session = r.json()
    assert set(session) == {"id", "approve_key", "poll_key", "expires_at"}
    assert session["expires_at"] == pytest.approx(time.time() + 300, abs=5)
    sid = session["id"]
    poll = bearer(session["poll_key"])
    r = client.get(f"/v1/web-sessions/{sid}", headers=poll)
    assert r.status_code == 200 and r.json() == {"status": "pending", "expires_at": session["expires_at"]}
    r = client.post(f"/v1/web-sessions/{sid}/inspect", json={"approve_key": session["approve_key"]}, headers=headers)
    assert r.status_code == 200, r.text
    seen = r.json()
    assert set(seen) == {"name", "user_agent", "country", "created_at", "expires_at"}
    assert (seen["name"], seen["user_agent"], seen["country"]) == ("Chrome on macOS", "Mozilla/5.0 (Macintosh)", None)
    r = client.post(f"/v1/web-sessions/{sid}/approve", json={"approve_key": session["approve_key"]}, headers=headers)
    assert r.status_code == 200 and r.json() == {"approved": True}
    r = client.post(f"/v1/web-sessions/{sid}/approve", json={"approve_key": session["approve_key"]}, headers=headers)
    assert r.status_code == 404
    r = client.get(f"/v1/web-sessions/{sid}", headers=poll)
    assert r.status_code == 200, r.text
    web = r.json()
    assert web["status"] == "approved" and set(web) == SESSION_FIELDS | {"status"}
    assert web["account"] == phone["account"]
    devices = client.get("/v1/devices", headers=bearer(web["access_token"])).json()["devices"]
    assert {(d["name"], d["platform"]) for d in devices} == {("phone", "ios"), ("Chrome on macOS", "web")}
    r = client.get(f"/v1/web-sessions/{sid}", headers=poll)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_poll_needs_the_poll_key(client):
    every_two_seconds(client)
    phone = create_account(client)
    headers = bearer(phone["access_token"])
    session = web_session(client)
    sid = session["id"]
    r = client.get(f"/v1/web-sessions/{sid}", headers=bearer(session["approve_key"]))
    assert r.status_code == 404 and session["approve_key"] not in r.text
    r = client.post(f"/v1/web-sessions/{sid}/inspect", json={"approve_key": session["poll_key"]}, headers=headers)
    assert r.status_code == 404
    r = client.post(f"/v1/web-sessions/{sid}/approve", json={"approve_key": session["poll_key"]}, headers=headers)
    assert r.status_code == 404
    assert client.post(f"/v1/web-sessions/{sid}/approve", json={"approve_key": session["approve_key"]},
                       headers=headers).status_code == 200
    # Whoever saw the QR (the approve key) still cannot get the session.
    assert client.get(f"/v1/web-sessions/{sid}", headers=bearer(session["approve_key"])).status_code == 404
    assert client.get(f"/v1/web-sessions/{sid}", headers=bearer(session["poll_key"])).json()["status"] == "approved"


def test_poll_without_a_key_is_401(client):
    session = web_session(client)
    assert client.get(f"/v1/web-sessions/{session['id']}").status_code == 401


def test_poll_every_two_seconds_is_not_limited(client):
    clock = [1000.0]
    client.app.state.poll_limiter = PollLimiter(now=lambda: clock[0])
    session = web_session(client)
    url, poll = f"/v1/web-sessions/{session['id']}", bearer(session["poll_key"])
    for _ in range(40):
        r = client.get(url, headers=poll)
        assert r.status_code == 200, r.text
        clock[0] += 2
    assert client.get(url, headers=poll).status_code == 200
    r = client.get(url, headers=poll)
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"


def test_watch_cannot_approve_web_sessions(client):
    phone = create_account(client)
    watch = enroll(client, enroll_code(client, phone)).json()
    session = web_session(client)
    body = {"approve_key": session["approve_key"]}
    for action in ("inspect", "approve"):
        r = client.post(f"/v1/web-sessions/{session['id']}/{action}", json=body, headers=bearer(watch["access_token"]))
        assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert client.get(f"/v1/web-sessions/{session['id']}", headers=bearer(session["poll_key"])).json()["status"] == (
        "pending"
    )


def test_web_session_must_be_web(client):
    for platform in ("ios", "watchos", "macos", "nope"):
        r = client.post("/v1/web-sessions", json={"device": {"name": "x", "platform": platform}})
        assert r.status_code == 422 and r.json()["error"] == "invalid"
    assert client.post("/v1/web-sessions", json={}).status_code == 422
    assert client.post("/v1/web-sessions", json={"device": {"platform": "web"}}).status_code == 422


def test_web_session_user_agent_is_cut_and_cleaned(client):
    phone = create_account(client)
    session = web_session(client, headers={"User-Agent": "A\tB" + "x" * 300})
    r = client.post(f"/v1/web-sessions/{session['id']}/inspect", json={"approve_key": session["approve_key"]},
                    headers=bearer(phone["access_token"]))
    user_agent = r.json()["user_agent"]
    assert user_agent.startswith("AB") and len(user_agent) == 200


def test_inspect_shows_the_country(config, mongo_db, fake_verifier, signing_key):
    for c in served(replace(config, client_country_header="CF-IPCountry"), mongo_db, fake_verifier, signing_key):
        headers = bearer(create_account(c)["access_token"])
        for sent, shown in (("BR", "BR"), ("XX", None), ("T1", None), ("br", None), ("BRA", None)):
            session = web_session(c, headers={"CF-IPCountry": sent})
            r = c.post(f"/v1/web-sessions/{session['id']}/inspect", json={"approve_key": session["approve_key"]},
                       headers=headers)
            assert r.json()["country"] == shown, sent


def test_country_ignored_without_the_setting(client):
    headers = bearer(create_account(client)["access_token"])
    session = web_session(client, headers={"CF-IPCountry": "BR"})
    r = client.post(f"/v1/web-sessions/{session['id']}/inspect", json={"approve_key": session["approve_key"]},
                    headers=headers)
    assert r.json()["country"] is None


def test_approve_at_the_device_limit_is_403(config, mongo_db, fake_verifier, signing_key):
    for c in served(replace(config, max_devices=1), mongo_db, fake_verifier, signing_key):
        phone = create_account(c)
        session = web_session(c)
        r = c.post(f"/v1/web-sessions/{session['id']}/approve", json={"approve_key": session["approve_key"]},
                   headers=bearer(phone["access_token"]))
        assert r.status_code == 403 and r.json()["error"] == "limit"
        assert c.get(f"/v1/web-sessions/{session['id']}", headers=bearer(session["poll_key"])).json()["status"] == (
            "pending"
        )


# Invites


def invite(client, plan: str = "full") -> str:
    return client.portal.call(functools.partial(client.app.state.accounts.create_invites, plan, 1, days=30))[0]


def test_redeem_invite_sets_the_plan(client):
    web = create_account(client, "web")
    headers = bearer(web["access_token"])
    assert client.get("/v1/account", headers=headers).json()["plan"] is None
    r = client.post("/v1/entitlements/redeem", json={"code": invite(client, "custom")}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"plan": "custom", "source": "beta", "expires_at": None}
    assert client.get("/v1/account", headers=headers).json()["plan"] == {
        "plan": "custom",
        "source": "beta",
        "expires_at": None,
    }


def test_redeem_records_country(config, mongo_db, fake_verifier, signing_key):
    for c in served(replace(config, client_country_header="CF-IPCountry"), mongo_db, fake_verifier, signing_key):
        for sent, kept in (({"CF-IPCountry": "BR"}, "BR"), ({"CF-IPCountry": "XX"}, None), ({}, None)):
            phone = create_account(c)
            headers = {**bearer(phone["access_token"]), **sent}
            r = c.post("/v1/entitlements/redeem", json={"code": invite(c)}, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json() == {"plan": "full", "source": "beta", "expires_at": None}  # the country stays inside
            find = c.app.state.accounts.entitlements.find_one
            doc = c.portal.call(functools.partial(find, {"_id": phone["account"]}))
            assert doc["country"] == kept, sent


def test_redeem_twice_is_404(client):
    code = invite(client)
    first = bearer(create_account(client)["access_token"])
    second = bearer(create_account(client)["access_token"])
    assert client.post("/v1/entitlements/redeem", json={"code": code}, headers=first).status_code == 200
    r = client.post("/v1/entitlements/redeem", json={"code": code}, headers=second)
    assert r.status_code == 404 and r.json()["error"] == "invalid_code" and code not in r.text


def test_redeem_with_a_plan_is_409_and_keeps_the_invite(client):
    headers = bearer(create_account(client)["access_token"])
    assert client.post("/v1/entitlements/redeem", json={"code": invite(client)}, headers=headers).status_code == 200
    kept = invite(client, "custom")
    r = client.post("/v1/entitlements/redeem", json={"code": kept}, headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "already_entitled"
    other = bearer(create_account(client)["access_token"])
    r = client.post("/v1/entitlements/redeem", json={"code": kept}, headers=other)
    assert r.status_code == 200 and r.json()["plan"] == "custom"


def test_watch_cannot_redeem(client):
    phone = create_account(client)
    watch = enroll(client, enroll_code(client, phone)).json()
    r = client.post("/v1/entitlements/redeem", json={"code": invite(client)}, headers=bearer(watch["access_token"]))
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_attempt_limit_on_enroll_and_redeem(config, mongo_db, fake_verifier, signing_key):
    strict = replace(config, session_attempts_per_minute=2, client_ip_header="X-Forwarded-For")
    for c in served(strict, mongo_db, fake_verifier, signing_key):
        first, second = {"X-Forwarded-For": "198.51.100.1"}, {"X-Forwarded-For": "198.51.100.2"}
        body = {"code": "wce_nothing", "device": device("watchos")}
        for _ in range(2):
            assert c.post("/v1/devices/enroll", json=body, headers=first).status_code == 404
        r = c.post("/v1/devices/enroll", json=body, headers=first)
        assert r.status_code == 429 and 0 < int(r.headers["Retry-After"]) <= 60
        headers = {**second, **bearer(create_account(c)["access_token"])}
        for _ in range(2):
            r = c.post("/v1/entitlements/redeem", json={"code": "WC-AAAA-AAAA-AAAA"}, headers=headers)
            assert r.status_code == 404
        r = c.post("/v1/entitlements/redeem", json={"code": "WC-AAAA-AAAA-AAAA"}, headers=headers)
        assert r.status_code == 429
        # Web sessions count against the same per-IP attempts.
        assert c.post("/v1/web-sessions", json={"device": {"name": "w"}}, headers=first).status_code == 429
