from typing import Any

import pytest
from fastapi import Depends, Request
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import BaseModel
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
        "server_tokens": False,
        "push": {"apns": False, "webpush": False, "vapid_public_key": None, "apns_topics": []},
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


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


P256_PEM = _pem(ec.generate_private_key(ec.SECP256R1()))


def test_signing_key_and_public_url_go_together():
    message = "WRISTCALL_CLOUD_PUBLIC_URL and WRISTCALL_CLOUD_SIGNING_KEY go together"
    with pytest.raises(ConfigError, match=message):
        config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_SIGNING_KEY": P256_PEM.decode()})
    with pytest.raises(ConfigError, match=message):
        config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_PUBLIC_URL": "https://cloud.example"})
    cfg = config_from_env(
        {**BASE_ENV, "WRISTCALL_CLOUD_SIGNING_KEY": P256_PEM.decode(), "WRISTCALL_CLOUD_PUBLIC_URL": "https://cloud.example/"}
    )
    assert cfg.signing_key_pem == P256_PEM.strip()
    assert cfg.public_url == "https://cloud.example"
    assert cfg.server_token_ttl_s == 300
    assert cfg.allow_loopback_audience is False
    plain = config_from_env(BASE_ENV)
    assert plain.signing_key_pem is None and plain.public_url is None


def test_signing_key_from_file(tmp_path):
    path = tmp_path / "signing.pem"
    path.write_bytes(P256_PEM)
    env = {**BASE_ENV, "WRISTCALL_CLOUD_SIGNING_KEY_FILE": str(path), "WRISTCALL_CLOUD_PUBLIC_URL": "https://cloud.example"}
    assert config_from_env(env).signing_key_pem == P256_PEM
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_SIGNING_KEY") as e:
        config_from_env({**env, "WRISTCALL_CLOUD_SIGNING_KEY": P256_PEM.decode()})
    assert "PRIVATE KEY" not in str(e.value)
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_SIGNING_KEY_FILE") as e:
        config_from_env({**env, "WRISTCALL_CLOUD_SIGNING_KEY_FILE": str(tmp_path / "s3cr3t-missing.pem")})
    assert "s3cr3t" not in str(e.value)


def test_signing_key_must_be_p256():
    rsa_pem = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    p384_pem = _pem(ec.generate_private_key(ec.SECP384R1()))
    for pem in (rsa_pem, p384_pem, b"s3cr3t-not-a-pem"):
        with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_SIGNING_KEY") as e:
            config_from_env(
                {**BASE_ENV, "WRISTCALL_CLOUD_SIGNING_KEY": pem.decode(), "WRISTCALL_CLOUD_PUBLIC_URL": "https://c.example"}
            )
        assert "s3cr3t" not in str(e.value) and "PRIVATE KEY" not in str(e.value)


def test_public_url_differs_from_issuer():
    env = {**BASE_ENV, "WRISTCALL_CLOUD_SIGNING_KEY": P256_PEM.decode(), "WRISTCALL_CLOUD_ISSUER": "https://auth.example"}
    for public_url in ("https://auth.example", "https://auth.example/", "HTTPS://Auth.Example:443"):
        with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_PUBLIC_URL"):
            config_from_env({**env, "WRISTCALL_CLOUD_PUBLIC_URL": public_url})
    for public_url in ("http://cloud.example", "cloud.example", "https://u:s3cr3t@cloud.example"):
        with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_PUBLIC_URL") as e:
            config_from_env({**env, "WRISTCALL_CLOUD_PUBLIC_URL": public_url})
        assert "s3cr3t" not in str(e.value)


@pytest.mark.parametrize("public_url", ["HTTPS://Cloud.Example", "https://cloud.example:443", "https://cloud.example/a//b"])
def test_public_url_must_be_canonical(public_url):
    # The URL is the `iss` of every per-server token: servers compare it byte for byte.
    env = {**BASE_ENV, "WRISTCALL_CLOUD_SIGNING_KEY": P256_PEM.decode()}
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_PUBLIC_URL"):
        config_from_env({**env, "WRISTCALL_CLOUD_PUBLIC_URL": public_url})


@pytest.mark.parametrize("public_url", ["https://cloud.example/api", "http://localhost:8081", "http://127.0.0.1:8081"])
def test_public_url_canonical_forms_are_accepted(public_url):
    env = {**BASE_ENV, "WRISTCALL_CLOUD_SIGNING_KEY": P256_PEM.decode()}
    assert config_from_env({**env, "WRISTCALL_CLOUD_PUBLIC_URL": public_url}).public_url == public_url


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("0", False), ("", False)])
def test_allow_loopback_audience_from_env(raw, expected):
    assert config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE": raw}).allow_loopback_audience is expected


def test_allow_loopback_audience_rejects_other_values():
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE"):
        config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE": "maybe"})


def test_missing_token_is_401(probe):
    r = probe.get("/_probe")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    for header in ("Basic abc", "Bearer ", "Bearer"):
        r = probe.get("/_probe", headers={"Authorization": header})
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


def test_big_chunked_body_is_413_on_model_routes(app, client):
    # FastAPI turns any non-HTTPException raised while reading a model body into 400; the limit must survive that.
    class Blob(BaseModel):
        blob: str

    @app.post("/_probe_model")
    async def model(body: Blob) -> dict[str, int]:
        return {"size": len(body.blob)}

    def chunks():
        yield b'{"blob": "'
        for _ in range(70):
            yield b"x" * 1024
        yield b'"}'

    r = client.post("/_probe_model", headers={"Content-Type": "application/json"}, content=chunks())
    assert r.status_code == 413
    assert r.json() == {"error": "too_large", "message": "request body is limited to 65536 bytes"}
    assert client.post("/_probe_model", json={"blob": "xyz"}).json() == {"size": 3}


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
