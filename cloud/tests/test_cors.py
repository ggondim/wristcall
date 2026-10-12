"""CORS for listed browser origins (the wristcall PWA): nothing is open unless an origin is listed."""

from dataclasses import replace

import pytest

from conftest import serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import ConfigError, config_from_env
from wristcall_cloud.store import Store

ORIGIN = "https://app.example"
BASE_ENV = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017", "WRISTCALL_CLOUD_CLIENT_IOS": "client-ios"}
VAR = "WRISTCALL_CLOUD_CORS_ORIGINS"


@pytest.fixture
def cors_client(config, mongo_db, fake_verifier):
    app = create_app(replace(config, cors_origins=(ORIGIN,)), store=Store(mongo_db), verifier=fake_verifier)
    yield from serve(app, mongo_db)


def test_preflight_allows_listed_origin(cors_client):
    r = cors_client.options(
        "/v1/servers",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "content-type" in r.headers["access-control-allow-headers"].lower()
    assert r.headers["access-control-max-age"] == "600"
    assert "access-control-allow-credentials" not in r.headers


def test_preflight_methods(cors_client):
    r = cors_client.options("/v1/servers", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "PATCH"})
    assert r.status_code == 200
    allowed = {m.strip() for m in r.headers["access-control-allow-methods"].split(",")}
    assert allowed == {"GET", "POST", "PUT", "PATCH", "DELETE"}


def test_preflight_refuses_other_origin(cors_client):
    r = cors_client.options(
        "/v1/servers", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in r.headers


def test_preflight_refuses_other_header(cors_client):
    r = cors_client.options(
        "/v1/servers",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-evil"},
    )
    assert r.status_code == 400


def test_error_answers_carry_cors(cors_client):
    r = cors_client.get("/v1/servers", headers={"Origin": ORIGIN})  # no token: 401
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == ORIGIN
    r = cors_client.get("/v1/nope", headers={"Origin": ORIGIN})
    assert r.status_code == 404
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_validation_error_carries_cors(cors_client, fake_verifier):
    fake_verifier.add("token-a", "user-a")
    r = cors_client.post(
        "/v1/servers", content=b"[]", headers={"Origin": ORIGIN, "Authorization": "Bearer token-a", "Content-Type": "application/json"}
    )
    assert r.status_code == 422
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_too_large_carries_cors(cors_client):
    r = cors_client.post(
        "/v1/servers",
        content=b"x" * 70_000,
        headers={"Origin": ORIGIN, "Authorization": "Bearer token-a", "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_other_origin_gets_no_cors_headers(cors_client):
    r = cors_client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_push_relay_registration_preflight(cors_client):
    r = cors_client.options(
        "/v1/push/registrations",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_retry_after_is_exposed(cors_client):
    r = cors_client.get("/v1/health", headers={"Origin": ORIGIN})
    assert "retry-after" in r.headers["access-control-expose-headers"].lower()


def test_no_origins_no_cors(client):
    r = client.get("/v1/health", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    r = client.options("/v1/servers", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_origins_parsed():
    config = config_from_env({**BASE_ENV, VAR: "https://A.example, http://localhost:5173"})
    assert config.cors_origins == ("https://a.example", "http://localhost:5173")


def test_cors_origins_default_and_blank_items():
    assert config_from_env(BASE_ENV).cors_origins == ()
    assert config_from_env({**BASE_ENV, VAR: " , "}).cors_origins == ()
    assert config_from_env({**BASE_ENV, VAR: "https://a.example,,https://a.example"}).cors_origins == ("https://a.example",)


@pytest.mark.parametrize(
    "http_origin", ["http://localhost", "http://127.0.0.1:8080", "http://[::1]:5173"]
)
def test_cors_origin_allows_http_on_loopback(http_origin):
    assert config_from_env({**BASE_ENV, VAR: http_origin}).cors_origins == (http_origin,)


def test_cors_origin_drops_default_port():
    config = config_from_env({**BASE_ENV, VAR: "https://a.example:443"})
    assert config.cors_origins == ("https://a.example",)


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "https://a.example/",
        "https://a.example/app",
        "http://a.example",
        "https://u:p@a.example",
        "ftp://a.example",
        "https://a.example?x=1",
        "https://a.example#f",
        "https://a.example:99999",
        "https://a.example:abc",
        "a.example",
        "https:///x",
        "https://a.example\\b",
    ],
)
def test_cors_origin_rejects(value):
    with pytest.raises(ConfigError, match=VAR) as e:
        config_from_env({**BASE_ENV, VAR: value})
    assert value not in str(e.value)
