"""CORS for listed browser origins (the wristcall PWA): nothing is open unless an origin is listed."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wristcall.app import create_app
from wristcall.config import ConfigError, load_config, parse_config
from wristcall.storage import open_sqlite_storage
from wristcall.users import UserService

ORIGIN = "https://app.example"


def config_with(origins=None):
    server = {"public_url": "http://testserver"}
    if origins is not None:
        server["cors_origins"] = origins
    return parse_config(
        {
            "server": server,
            "providers": {
                "stt": {"type": "fake_stt", "text": "hi"},
                "llm": {"type": "echo_chat"},
                "tts": {"type": "tone_tts", "sample_rate": 16000},
            },
            "profiles": {
                "default": {"display_name": "Test", "stt": "stt", "responder": "llm", "tts": "tts", "system_prompt": "Be brief."}
            },
        },
        {},
    )


def make_client(origins=None):
    return TestClient(create_app(config_with(origins), storage=open_sqlite_storage(":memory:")))


@pytest.fixture
def cors_client():
    with make_client([ORIGIN]) as c:
        yield c


@pytest.fixture
def plain_client():
    with make_client() as c:
        yield c


def api_token(client) -> str:
    users = UserService(client.app.state.storage)
    user = asyncio.run(client.app.state.storage.users.by_handle("owner")) or asyncio.run(users.create("owner"))
    return asyncio.run(users.issue_token(user.id, "test"))[1]


def test_preflight_allows_listed_origin(cors_client):
    r = cors_client.options(
        "/v1/agents",
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
    r = cors_client.options("/v1/agents", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "PATCH"})
    assert r.status_code == 200
    allowed = {m.strip() for m in r.headers["access-control-allow-methods"].split(",")}
    assert allowed == {"GET", "POST", "PUT", "PATCH", "DELETE"}


def test_preflight_refuses_other_origin(cors_client):
    r = cors_client.options(
        "/v1/agents", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in r.headers


def test_preflight_refuses_other_header(cors_client):
    r = cors_client.options(
        "/v1/agents",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-evil"},
    )
    assert r.status_code == 400


def test_error_answers_carry_cors(cors_client):
    r = cors_client.get("/v1/agents", headers={"Origin": ORIGIN})  # no token: 401
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == ORIGIN
    r = cors_client.get("/v1/nope", headers={"Origin": ORIGIN})
    assert r.status_code == 404
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_validation_error_carries_cors(cors_client):
    t = api_token(cors_client)
    r = cors_client.post(
        "/v1/agents", json={"slug": "Bad"}, headers={"Origin": ORIGIN, "Authorization": f"Bearer {t}"}
    )
    assert r.status_code == 422
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_other_origin_gets_no_cors_headers(cors_client):
    r = cors_client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_export_exposes_content_disposition(cors_client):
    t = api_token(cors_client)
    r = cors_client.get("/v1/calls/export?format=md", headers={"Origin": ORIGIN, "Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "content-disposition" in r.headers["access-control-expose-headers"].lower()


def test_websocket_call_route_is_unaffected(cors_client):
    t = api_token(cors_client)  # an API token is not a device: the call socket closes 4401, CORS or not
    with cors_client.websocket_connect("/v1/call", headers={"Origin": ORIGIN, "Authorization": f"Bearer {t}"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401


def test_no_origins_no_cors(plain_client):
    r = plain_client.get("/v1/health", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    r = plain_client.options("/v1/agents", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_origins_parsed_and_defaults():
    assert config_with().server.cors_origins == []
    cfg = config_with(["https://A.example", " http://localhost:5173 ", "https://a.example:443", "http://[::1]:5173"])
    assert cfg.server.cors_origins == ["https://a.example", "http://localhost:5173", "http://[::1]:5173"]


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
    ],
)
def test_cors_origin_rejects(tmp_path, value):
    path = tmp_path / "wristcall.yaml"
    path.write_text(
        json.dumps(
            {
                "server": {"public_url": "http://testserver", "cors_origins": [value]},
                "providers": {"stt": {"type": "fake_stt"}, "llm": {"type": "echo_chat"}, "tts": {"type": "tone_tts"}},
                "profiles": {"default": {"display_name": "T", "stt": "stt", "responder": "llm", "tts": "tts", "system_prompt": "x"}},
            }
        )
    )
    with pytest.raises(ConfigError, match="server.cors_origins") as e:
        load_config(path, {})
    assert value not in str(e.value)
