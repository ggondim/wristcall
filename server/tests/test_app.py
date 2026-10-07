import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import fake_config, silence, tone
from wristcall.app import create_app
from wristcall.pairing import PairingService
from wristcall.store import Database

START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}


@pytest.fixture
def svc():
    return PairingService(Database(":memory:"))


@pytest.fixture
def client(svc):
    with TestClient(create_app(fake_config(), pairing=svc)) as c:
        yield c


def pair(client, svc) -> str:
    code = svc.create_code().code
    r = client.post("/v1/pair", json={"code": code, "device_name": "Watch"})
    assert r.status_code == 200
    return r.json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    assert client.get("/v1/health").json() == {"status": "ok", "version": "0.2.0", "protocol": 1}


def test_flow_a_me_and_unpair(client, svc):
    token = pair(client, svc)
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["device_name"] == "Watch"
    assert me["profiles"] == [{"name": "default", "display_name": "Test"}]
    assert client.delete("/v1/me", headers=auth(token)).status_code == 204
    assert client.get("/v1/me", headers=auth(token)).status_code == 401
    assert client.get("/v1/me").status_code == 401


def test_invalid_code(client):
    r = client.post("/v1/pair", json={"code": "0000 0000", "device_name": "x"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_code"


def test_flow_b_over_http():
    svc = PairingService(Database(":memory:"), "manual")
    with TestClient(create_app(fake_config(), pairing=svc)) as client:
        r = client.post("/v1/pair", json={"code": "12345678", "device_name": "Watch"})
        assert r.status_code == 202
        body = r.json()
        poll = {"poll_token": body["poll_token"]}
        r = client.post("/v1/pair/poll", json=poll)
        assert r.status_code == 202
        assert r.json() == {"request_id": body["request_id"], "expires_at": body["expires_at"]}
        svc.approve(body["request_id"])
        r = client.post("/v1/pair/poll", json=poll)
        assert r.status_code == 200 and r.json()["token"] and r.json()["device_id"]
        r = client.post("/v1/pair/poll", json=poll)
        assert r.status_code == 410 and r.json()["error"] == "gone"


def test_poll_token_is_not_in_the_path():
    """The poll_token is a secret: it goes in the POST body, not in the path (which the access log records)."""
    svc = PairingService(Database(":memory:"), "manual")
    with TestClient(create_app(fake_config(), pairing=svc)) as client:
        token = client.post("/v1/pair", json={"device_name": "Watch"}).json()["poll_token"]
        assert client.get(f"/v1/pair/{token}").status_code in (404, 405)
        assert client.get("/v1/pair/poll").status_code in (404, 405)


def test_poll_validates_body(client):
    assert client.post("/v1/pair/poll", json={}).status_code == 422
    assert client.post("/v1/pair/poll", json={"poll_token": "x" * 129}).status_code == 422
    r = client.post("/v1/pair/poll", json={"poll_token": "doesnotexist"})
    assert r.status_code == 410 and r.json()["error"] == "gone"


def test_pair_rate_limit(client):
    codes = [client.post("/v1/pair", json={"code": "11111111", "device_name": "x"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def read_until_agent_end(ws) -> list[str]:
    events = []
    while True:
        m = ws.receive()
        if m.get("bytes"):
            events.append("audio")
            continue
        msg = json.loads(m["text"])
        events.append(msg["type"])
        if msg["type"] == "turn.agent_end":
            return events


def test_call_round_trip(client, svc):
    token = pair(client, svc)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json(START)
        ready = ws.receive_json()
        assert ready["type"] == "session.ready"
        assert ready["profile"] == {"name": "default", "display_name": "Test"}
        assert ready["audio_out"] == {"codec": "pcm16", "sample_rate": 16000, "channels": 1}
        ws.send_bytes(tone(500) + silence(900))
        events = read_until_agent_end(ws)
        assert events[:3] == ["turn.user_end", "transcript", "turn.agent_start"]
        assert "audio" in events
        ws.send_json({"type": "session.end"})


def test_manual_call_ends_turn_on_mute_not_on_silence(client, svc):
    """3 s of silence after speech would close an auto turn by "vad"; in manual only the mute closes it."""
    token = pair(client, svc)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "turn_end": "manual"})
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_bytes(tone(500) + silence(3000))
        ws.send_json({"type": "mute", "muted": True})
        assert ws.receive_json() == {"type": "turn.user_end", "reason": "mute"}
        events = read_until_agent_end(ws)
        assert events[:2] == ["transcript", "turn.agent_start"]
        ws.send_json({"type": "session.end"})


def test_bad_message_mid_call_is_not_fatal(client, svc):
    token = pair(client, svc)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json(START)
        ws.receive_json()
        ws.send_text("this is not json")
        err = ws.receive_json()
        assert err["code"] == "bad_message" and err["fatal"] is False
        ws.send_json({"type": "mute", "muted": True})
        ws.send_json({"type": "session.end"})


def expect_close(ws, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as e:
        ws.receive_text()
    assert e.value.code == code


def test_ws_rejects_missing_or_revoked_token(client, svc):
    with client.websocket_connect("/v1/call") as ws:
        expect_close(ws, 4401)
    token = pair(client, svc)
    client.delete("/v1/me", headers=auth(token))
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        expect_close(ws, 4401)


@pytest.mark.parametrize(
    "first, code",
    [
        ({"type": "mute", "muted": True}, "not_started"),
        ({**START, "protocol": 2}, "unsupported_protocol"),
        ({**START, "profile": "does_not_exist"}, "unknown_profile"),
        ({**START, "audio_in": {"codec": "pcm16", "sample_rate": 8000, "channels": 1}}, "unsupported_audio"),
        ({**START, "turn_end": "push_to_talk"}, "bad_message"),
    ],
)
def test_ws_fatal_opening_errors(client, svc, first, code):
    token = pair(client, svc)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json(first)
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == code and err["fatal"] is True
        expect_close(ws, 4400)


def test_ws_binary_before_start_is_not_started(client, svc):
    token = pair(client, svc)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_bytes(silence(20))
        assert ws.receive_json()["code"] == "not_started"
        expect_close(ws, 4400)
