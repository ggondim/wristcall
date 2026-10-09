from typing import Any

import pytest
from fastapi import Depends, Request
from pymongo import MongoClient

from conftest import ISSUER, mongo_url, serve
from wristcall_cloud import __version__
from wristcall_cloud.app import Caller, current_account, json_object
from wristcall_cloud.config import CloudConfig, ConfigError, config_from_env

BASE_ENV = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017", "WRISTCALL_CLOUD_CLIENT_IOS": "client-ios"}


@pytest.fixture
def probe(app, client):
    @app.get("/_probe")
    async def who(caller: Caller = Depends(current_account)) -> dict[str, Any]:
        return {"key": caller.key, "client_id": caller.client_id}

    @app.post("/_probe")
    async def echo(request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
        return await json_object(request)

    return client


def test_health(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_config_is_public_and_has_clients(client):
    r = client.get("/v1/config")
    assert r.status_code == 200
    assert r.json() == {
        "issuer": ISSUER,
        "project_id": "1234",
        "clients": {"ios": "client-ios", "pwa": "client-pwa", "watch": "client-watch"},
        "scopes": ["openid", "profile", "offline_access", "urn:zitadel:iam:org:project:id:1234:aud"],
    }


def test_config_without_project_id_has_no_project_scope(mongo_db, fake_verifier):
    from wristcall_cloud.app import create_app
    from wristcall_cloud.store import Store

    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients={"ios": "client-ios"})
    for c in serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier), mongo_db):
        body = c.get("/v1/config").json()
    assert body["project_id"] is None
    assert body["scopes"] == ["openid", "profile", "offline_access"]


def test_config_from_env_requires_mongo_and_a_client():
    cfg = config_from_env(BASE_ENV)
    assert cfg.mongo_url == "mongodb://localhost:27017"
    assert cfg.clients == {"ios": "client-ios"}
    assert cfg.database == "wristcall_cloud"
    assert cfg.issuer == "https://auth.trigram.com.br"
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_MONGO_URL"):
        config_from_env({"WRISTCALL_CLOUD_CLIENT_IOS": "client-ios"})
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_CLIENT_"):
        config_from_env({"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017"})


def test_config_from_env_reads_everything():
    cfg = config_from_env(
        {
            **BASE_ENV,
            "WRISTCALL_CLOUD_DATABASE": "cloud",
            "WRISTCALL_CLOUD_ISSUER": "https://issuer.example/",
            "WRISTCALL_CLOUD_CLIENT_PWA": "client-pwa",
            "WRISTCALL_CLOUD_CLIENT_WATCH": "client-watch",
            "WRISTCALL_CLOUD_PROJECT_ID": "99",
            "WRISTCALL_CLOUD_MAX_SERVERS": "5",
            "WRISTCALL_CLOUD_MAX_AGENTS_PER_SERVER": "7",
        }
    )
    assert cfg == CloudConfig(
        mongo_url="mongodb://localhost:27017",
        database="cloud",
        issuer="https://issuer.example",
        clients={"ios": "client-ios", "pwa": "client-pwa", "watch": "client-watch"},
        project_id="99",
        max_servers=5,
        max_agents_per_server=7,
    )


def test_config_from_env_rejects_http_issuer():
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_ISSUER"):
        config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_ISSUER": "http://auth.example"})
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_ISSUER"):
        config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_ISSUER": "auth.example"})
    assert config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_ISSUER": "http://localhost:8081/"}).issuer == (
        "http://localhost:8081"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"WRISTCALL_CLOUD_MONGO_URL": "postgres://admin:s3cr3t-pass@db.example/x"},
        {"WRISTCALL_CLOUD_ISSUER": "http://admin:s3cr3t-pass@auth.example"},
        {"WRISTCALL_CLOUD_MAX_SERVERS": "s3cr3t-pass"},
        {"WRISTCALL_CLOUD_MAX_AGENTS_PER_SERVER": "-s3cr3t-pass"},
    ],
)
def test_config_errors_do_not_echo_values(overrides):
    with pytest.raises(ConfigError) as e:
        config_from_env({**BASE_ENV, **overrides})
    assert "s3cr3t-pass" not in str(e.value)


def test_missing_token_is_401(probe):
    r = probe.get("/_probe")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    r = probe.get("/_probe", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_bad_token_is_401(probe, fake_verifier):
    fake_verifier.add("good", "user-1")
    r = probe.get("/_probe", headers={"Authorization": "Bearer forged"})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized", "message": "missing or invalid token"}


def test_good_token_gives_the_account_key(probe, fake_verifier):
    fake_verifier.add("good", "user-1", client_id="client-watch")
    r = probe.get("/_probe", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json() == {"key": f"{ISSUER}#user-1", "client_id": "client-watch"}


def test_issuer_down_is_503(probe, fake_verifier):
    fake_verifier.add("good", "user-1")
    fake_verifier.down = True
    r = probe.get("/_probe", headers={"Authorization": "Bearer good"})
    assert r.status_code == 503
    assert r.json()["error"] == "account_unavailable"
    assert "good" not in r.text


def test_big_body_is_413(probe, fake_verifier):
    fake_verifier.add("good", "user-1")
    auth = {"Authorization": "Bearer good"}
    r = probe.post("/_probe", headers=auth, json={"blob": "x" * (64 * 1024)})
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"

    def chunks():  # no Content-Length: the limit applies to what is actually read
        yield b'{"blob": "'
        for _ in range(70):
            yield b"x" * 1024
        yield b'"}'

    r = probe.post("/_probe", headers={**auth, "Content-Type": "application/json"}, content=chunks())
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"
    assert probe.post("/_probe", headers=auth, json={"blob": "x" * 1024}).status_code == 200


def test_invalid_json_is_422(probe, fake_verifier):
    fake_verifier.add("good", "user-1")
    auth = {"Authorization": "Bearer good", "Content-Type": "application/json"}
    for body in (b"{not json", b"[1, 2]", b'"text"'):
        r = probe.post("/_probe", headers=auth, content=body)
        assert r.status_code == 422
        assert r.json()["error"] == "invalid"


def test_unknown_route_has_the_error_shape(client):
    r = client.get("/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert "message" in r.json()


def test_indexes_are_created(client, mongo_db):
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        indexes = list(sync[mongo_db.name]["servers"].list_indexes())
    keys = {tuple(ix["key"].items()): ix.get("unique", False) for ix in indexes}
    assert keys[(("account", 1), ("url", 1))] is True
    assert (("account", 1), ("created_at", 1)) in keys
