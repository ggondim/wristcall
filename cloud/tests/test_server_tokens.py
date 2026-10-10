import dataclasses
import logging
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import CLIENTS, ISSUER, mongo_url, serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.oidc import OidcError
from wristcall_cloud.signing import SigningKey, server_token_claims
from wristcall_cloud.store import Store

H_A = {"Authorization": "Bearer token-a"}


def new_key() -> SigningKey:
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return SigningKey(pem)


@pytest.fixture(autouse=True)
def tokens(fake_verifier):
    fake_verifier.add("token-a", "a", client_id="client-ios")


@pytest.fixture
def signing_key() -> SigningKey:
    return new_key()


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), public_url="https://cloud.test")


@pytest.fixture
def app(config, mongo_db, fake_verifier, signing_key):
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)


def test_discovery_and_jwks(client):
    d = client.get("/.well-known/openid-configuration").json()
    assert d["issuer"] == "https://cloud.test" and d["jwks_uri"] == "https://cloud.test/v1/jwks"
    keys = client.get("/v1/jwks").json()["keys"]
    assert len(keys) == 1 and keys[0]["alg"] == "ES256" and keys[0]["use"] == "sig" and "d" not in keys[0]


def test_server_token_is_bound_to_the_audience(client, signing_key):
    r = client.post("/v1/server-tokens", json={"audience": "HTTPS://Home.Example.com:443/"}, headers=H_A)
    assert r.status_code == 200
    body = r.json()
    assert body["audience"] == "https://home.example.com"
    jwk = jwt.PyJWK(client.get("/v1/jwks").json()["keys"][0])
    claims = jwt.decode(body["token"], jwk.key, algorithms=["ES256"], audience="https://home.example.com",
                        issuer="https://cloud.test")
    assert claims["sub"] == f"{ISSUER}#a" and claims["exp"] - claims["iat"] == 300
    assert claims["client_id"] == "client-ios"
    assert jwt.get_unverified_header(body["token"])["typ"] == "wc-server+jwt"
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(body["token"], jwk.key, algorithms=["ES256"], audience="https://other.example.com")


def test_server_token_is_not_a_cloud_credential(client):
    token = client.post("/v1/server-tokens", json={"audience": "https://h.test"}, headers=H_A).json()["token"]
    assert client.get("/v1/account", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_cloud_with_the_real_verifier_refuses_its_own_tokens(config, mongo_db, signing_key):
    # Even with the Cloud's own audience and the account issuer's name, a Cloud-signed token is not a credential
    # for the Cloud: the real verifier only trusts the issuer's keys.
    issuer_key = new_key()

    def issuer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        return httpx.Response(200, json={"keys": [issuer_key.jwk()]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(issuer))
    app = create_app(config, store=Store(mongo_db), http=http, signing_key=signing_key)
    now = time.time()
    forged = [
        signing_key.sign(server_token_claims(issuer=iss, account="a", client_id="client-ios", audience=aud, now=now))
        for iss in ("https://cloud.test", ISSUER)
        for aud in ("https://h.test", "client-ios")
    ]
    genuine = issuer_key.sign(server_token_claims(issuer=ISSUER, account="a", client_id="client-ios", audience="client-ios", now=now))
    for c in serve(app, mongo_db):
        assert c.get("/v1/account", headers={"Authorization": f"Bearer {genuine}"}).status_code == 200
        for token in forged:
            assert c.get("/v1/account", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        c.portal.call(http.aclose)


async def test_cloud_tokens_pass_the_server_verifier(app):
    # Interop: the verifier the server uses (cloud keeps a byte-identical copy) accepts what the Cloud signs,
    # through the Cloud's own discovery and JWKS.
    from wristcall_cloud.oidc import OidcVerifier
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://cloud.test") as http:
        r = await http.post("/v1/server-tokens", json={"audience": "https://home.example.com"}, headers=H_A)
        verifier = OidcVerifier("https://cloud.test", ["https://home.example.com"], http)
        identity = await verifier.verify(r.json()["token"])
        assert identity.subject == f"{ISSUER}#a" and identity.client_id == "client-ios"
        other = OidcVerifier("https://cloud.test", ["https://other.example.com"], http)
        with pytest.raises(OidcError):
            await other.verify(r.json()["token"])


@pytest.mark.parametrize("body", [
    {}, {"audience": 1}, {"audience": "ftp://x"}, {"audience": "https://x?q=1"}, [],
    {"audience": "http://192.168.0.10:8765"}, {"audience": "http://localhost:8765"},   # loopback without the flag
    {"audience": "https://cloud.test"}, {"audience": "https://auth.test"},             # the Cloud itself / the issuer
    {"audience": "HTTPS://Cloud.Test:443/"},
])
def test_bad_audience_is_422(client, body):
    r = client.post("/v1/server-tokens", json=body, headers=H_A)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid"
    assert "token" not in r.json()


def test_loopback_audience_with_flag(mongo_db, fake_verifier, config, signing_key):
    cfg = dataclasses.replace(config, allow_loopback_audience=True)
    for c in serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key), mongo_db):
        r = c.post("/v1/server-tokens", json={"audience": "http://localhost:8765/"}, headers=H_A)
        bad = c.post("/v1/server-tokens", json={"audience": "http://192.168.0.10:8765"}, headers=H_A)
    assert r.status_code == 200
    assert r.json()["audience"] == "http://localhost:8765"
    assert jwt.decode(r.json()["token"], options={"verify_signature": False})["aud"] == "http://localhost:8765"
    assert bad.status_code == 422


def test_server_tokens_need_an_account_token(client):
    r = client.post("/v1/server-tokens", json={"audience": "https://h.test"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    r = client.post("/v1/server-tokens", json={"audience": "https://h.test"}, headers={"Authorization": "Bearer forged"})
    assert r.status_code == 401


def test_issuer_down_is_503(client, fake_verifier):
    fake_verifier.down = True
    r = client.post("/v1/server-tokens", json={"audience": "https://h.test"}, headers=H_A)
    assert r.status_code == 503
    assert r.json()["error"] == "account_unavailable"


def test_response_shape_and_expiry(client):
    r = client.post("/v1/server-tokens", json={"audience": "https://h.test/wc/"}, headers=H_A)
    body = r.json()
    assert set(body) == {"token", "audience", "expires_at"}
    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert body["expires_at"] == claims["exp"]
    assert claims["iss"] == "https://cloud.test" and claims["aud"] == "https://h.test/wc"
    assert isinstance(claims["jti"], str) and len(claims["jti"]) == 32
    again = client.post("/v1/server-tokens", json={"audience": "https://h.test/wc"}, headers=H_A).json()
    assert jwt.decode(again["token"], options={"verify_signature": False})["jti"] != claims["jti"]


def test_server_tokens_write_nothing(client, mongo_db):
    from pymongo import MongoClient

    assert client.post("/v1/server-tokens", json={"audience": "https://h.test"}, headers=H_A).status_code == 200
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        db = sync[mongo_db.name]
        assert all(db[name].count_documents({}) == 0 for name in db.list_collection_names())


def test_jwks_is_cacheable(client, signing_key):
    r = client.get("/v1/jwks")
    assert r.headers["cache-control"] == "public, max-age=3600"
    assert r.json() == {"keys": [signing_key.jwk()]}
    assert r.json()["keys"][0]["kid"] == signing_key.kid
    d = client.get("/.well-known/openid-configuration").json()
    assert d["id_token_signing_alg_values_supported"] == ["ES256"] and d["response_types_supported"] == []


def test_config_announces_server_tokens(client):
    assert client.get("/v1/config").json()["server_tokens"] is True


def test_not_configured_without_signing_key(mongo_db, fake_verifier):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS))
    for c in serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier), mongo_db):
        responses = [
            c.get("/.well-known/openid-configuration"),
            c.get("/v1/jwks"),
            c.post("/v1/server-tokens", json={"audience": "https://h.test"}, headers=H_A),
            c.post("/v1/server-tokens", json={"audience": "https://h.test"}),
        ]
        config_body = c.get("/v1/config").json()
    for r in responses:
        assert r.status_code == 404
        assert r.json()["error"] == "not_configured"
    assert config_body["server_tokens"] is False


def test_key_without_public_url_is_a_config_error(mongo_db, fake_verifier, signing_key):
    from wristcall_cloud.config import ConfigError

    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS))
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_PUBLIC_URL"):
        create_app(cfg, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key)


def test_token_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.post("/v1/server-tokens", json={"audience": "https://home.example.com"}, headers=H_A)
    token = r.json()["token"]
    client.post("/v1/server-tokens", json={"audience": "https://home.example.com?x=1"}, headers=H_A)
    messages = [rec.getMessage() for rec in caplog.records]
    assert "server token issued (client client-ios)" in messages
    assert not any(token in m or "home.example.com" in m for m in messages)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
