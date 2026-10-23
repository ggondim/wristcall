import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import fake_config, silence, tone
from wristcall import __version__
from wristcall.app import create_app
from wristcall.config import parse_config
from wristcall.storage import open_sqlite_storage
from wristcall.users import UserService

START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}


def run(coro):
    # The storage is synchronous underneath, so its coroutines can run on the test thread.
    return asyncio.run(coro)


@pytest.fixture
def client():
    with TestClient(create_app(fake_config(), storage=open_sqlite_storage(":memory:"))) as c:
        yield c


def owner(client):
    return run(client.app.state.storage.users.by_handle("owner"))


def pair(client, user_id: str | None = None, name: str = "Watch") -> str:
    code = run(client.app.state.pairing.create_code(user_id or owner(client).id)).code
    r = client.post("/v1/pair", json={"code": code, "device_name": name})
    assert r.status_code == 200
    return r.json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    assert client.get("/v1/health").json() == {"status": "ok", "version": __version__, "protocol": 1, "account": None, "push": None}


def test_startup_imports_the_default_profile(client):
    user = owner(client)
    assert user.handle == "owner"
    agents = run(client.app.state.agents.list(user.id))
    assert [(a.slug, a.display_name) for a in agents] == [("default", "Test")]


def test_flow_a_me_and_unpair(client):
    token = pair(client)
    me = client.get("/v1/me", headers=auth(token)).json()
    assert me["device_name"] == "Watch"
    assert me["profiles"] == [{"name": "default", "display_name": "Test"}]
    assert me["user"]["handle"] == "owner"
    (agent,) = me["agents"]
    assert agent["slug"] == "default" and agent["icon"] == "waveform" and agent["call_type"] == "conversation"
    assert set(agent) == {"id", "slug", "display_name", "icon", "color", "call_type", "turn_end"}
    assert client.delete("/v1/me", headers=auth(token)).status_code == 204
    assert client.get("/v1/me", headers=auth(token)).status_code == 401
    assert client.get("/v1/me").status_code == 401


def test_me_lists_agents_in_order_default_first(client):
    user = owner(client)
    run(client.app.state.agents.create(user.id, {"slug": "coach", "display_name": "Coach"}))
    me = client.get("/v1/me", headers=auth(pair(client))).json()
    assert [p["name"] for p in me["profiles"]] == ["default", "coach"]
    assert [a["slug"] for a in me["agents"]] == ["default", "coach"]


def test_api_token_is_not_a_device(client):
    _, token = run(UserService(client.app.state.storage).issue_token(owner(client).id, "cli"))
    assert client.get("/v1/me", headers=auth(token)).status_code == 401
    assert client.delete("/v1/me", headers=auth(token)).status_code == 401
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        expect_close(ws, 4401)


def test_invalid_code(client):
    r = client.post("/v1/pair", json={"code": "0000 0000", "device_name": "x"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_code"


def test_flow_b_over_http():
    cfg = fake_config()
    cfg = cfg.model_copy(update={"server": cfg.server.model_copy(update={"pairing_approval": "manual"})})
    app = create_app(cfg, storage=open_sqlite_storage(":memory:"))
    with TestClient(app) as client:
        r = client.post("/v1/pair", json={"code": "12345678", "device_name": "Watch"})
        assert r.status_code == 202
        body = r.json()
        poll = {"poll_token": body["poll_token"]}
        r = client.post("/v1/pair/poll", json=poll)
        assert r.status_code == 202
        assert r.json() == {"request_id": body["request_id"], "expires_at": body["expires_at"]}
        run(app.state.pairing.approve(body["request_id"], owner(client).id))
        r = client.post("/v1/pair/poll", json=poll)
        assert r.status_code == 200 and r.json()["token"] and r.json()["device_id"]
        r = client.post("/v1/pair/poll", json=poll)
        assert r.status_code == 410 and r.json()["error"] == "gone"


def test_poll_token_is_not_in_the_path(client):
    """The poll_token is a secret: it goes in the POST body, not in the path (which the access log records)."""
    assert client.get("/v1/pair/whatever").status_code in (404, 405)
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


def test_call_round_trip(client):
    token = pair(client)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json(START)
        ready = ws.receive_json()
        assert ready["type"] == "session.ready"
        assert ready["profile"] == {"name": "default", "display_name": "Test"}
        assert ready["agent"]["slug"] == "default" and ready["turn_end"] == "auto"
        assert ready["audio_out"] == {"codec": "pcm16", "sample_rate": 16000, "channels": 1}
        ws.send_bytes(tone(500) + silence(900))
        events = read_until_agent_end(ws)
        assert events[:3] == ["turn.user_end", "transcript", "turn.agent_start"]
        assert "audio" in events
        ws.send_json({"type": "session.end"})


@pytest.mark.parametrize("key", ["profile", "agent", "agent_id"])
def test_call_picks_the_agent(client, key):
    user = owner(client)
    coach = run(client.app.state.agents.create(user.id, {"slug": "coach", "display_name": "Coach"}))
    ref = {"profile": {"profile": "coach"}, "agent": {"agent": "coach", "profile": "default"}, "agent_id": {"agent": coach.id}}[key]
    with client.websocket_connect("/v1/call", headers=auth(pair(client))) as ws:
        ws.send_json({**START, **ref})
        ready = ws.receive_json()
        assert ready["profile"] == {"name": "coach", "display_name": "Coach"} and ready["agent"]["id"] == coach.id
        ws.send_json({"type": "session.end"})


def test_call_without_reference_uses_the_first_agent(client):
    user = owner(client)
    run(client.app.state.agents.create(user.id, {"slug": "coach", "position": 0}))
    with client.websocket_connect("/v1/call", headers=auth(pair(client))) as ws:
        ws.send_json(START)
        assert ws.receive_json()["agent"]["slug"] == "coach"
        ws.send_json({"type": "session.end"})


def test_agent_turn_end_applies_when_the_client_names_the_agent(client):
    """3 s of silence does not close a turn of a manual agent called by `agent`."""
    user = owner(client)
    run(client.app.state.agents.update(user.id, "default", {"turn_end": "manual"}))
    with client.websocket_connect("/v1/call", headers=auth(pair(client))) as ws:
        ws.send_json({**START, "agent": "default"})
        assert ws.receive_json()["turn_end"] == "manual"
        ws.send_bytes(tone(500) + silence(3000))
        ws.send_json({"type": "mute", "muted": True})
        assert ws.receive_json() == {"type": "turn.user_end", "reason": "mute"}
        ws.send_json({"type": "session.end"})


@pytest.mark.parametrize("legacy", [{"profile": "default"}, {}])
def test_legacy_clients_keep_auto_when_they_omit_turn_end(client, legacy):
    """watch 0.1.0 omits turn_end when the user picks auto: a manual agent must not turn that into manual."""
    user = owner(client)
    run(client.app.state.agents.update(user.id, "default", {"turn_end": "manual"}))
    with client.websocket_connect("/v1/call", headers=auth(pair(client))) as ws:
        ws.send_json({**START, **legacy})
        assert ws.receive_json()["turn_end"] == "auto"
        ws.send_bytes(tone(500) + silence(900))
        assert ws.receive_json() == {"type": "turn.user_end", "reason": "vad"}
        ws.send_json({"type": "session.end"})


def test_client_turn_end_wins_over_the_agent(client):
    user = owner(client)
    run(client.app.state.agents.update(user.id, "default", {"turn_end": "manual"}))
    with client.websocket_connect("/v1/call", headers=auth(pair(client))) as ws:
        ws.send_json({**START, "turn_end": "auto"})
        assert ws.receive_json()["turn_end"] == "auto"
        ws.send_bytes(tone(500) + silence(900))
        assert ws.receive_json() == {"type": "turn.user_end", "reason": "vad"}
        ws.send_json({"type": "session.end"})


def test_manual_call_ends_turn_on_mute_not_on_silence(client):
    """3 s of silence after speech would close an auto turn by "vad"; in manual only the mute closes it."""
    token = pair(client)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "turn_end": "manual"})
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_bytes(tone(500) + silence(3000))
        ws.send_json({"type": "mute", "muted": True})
        assert ws.receive_json() == {"type": "turn.user_end", "reason": "mute"}
        events = read_until_agent_end(ws)
        assert events[:2] == ["transcript", "turn.agent_start"]
        ws.send_json({"type": "session.end"})


def test_bad_message_mid_call_is_not_fatal(client):
    token = pair(client)
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


def expect_fatal(client, token: str, first: dict, code: str) -> dict:
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json(first)
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == code and err["fatal"] is True
        expect_close(ws, 4400)
    return err


def test_ws_rejects_missing_or_revoked_token(client):
    with client.websocket_connect("/v1/call") as ws:
        expect_close(ws, 4401)
    token = pair(client)
    client.delete("/v1/me", headers=auth(token))
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        expect_close(ws, 4401)


@pytest.mark.parametrize(
    "first, code",
    [
        ({"type": "mute", "muted": True}, "not_started"),
        ({**START, "protocol": 2}, "unsupported_protocol"),
        ({**START, "profile": "does_not_exist"}, "unknown_profile"),
        ({**START, "agent": "does_not_exist"}, "unknown_profile"),
        ({**START, "audio_in": {"codec": "pcm16", "sample_rate": 8000, "channels": 1}}, "unsupported_audio"),
        ({**START, "turn_end": "push_to_talk"}, "bad_message"),
        ({**START, "turn_end": None}, "bad_message"),
    ],
)
def test_ws_fatal_opening_errors(client, first, code):
    expect_fatal(client, pair(client), first, code)


def test_other_users_agents_are_unknown(client):
    storage = client.app.state.storage
    bob = run(UserService(storage).create("bob"))
    alice_agent = run(client.app.state.agents.list(owner(client).id))[0]
    bob_token = pair(client, bob.id)
    expect_fatal(client, bob_token, START, "unknown_profile")  # bob has no agents
    expect_fatal(client, bob_token, {**START, "agent": alice_agent.id}, "unknown_profile")


def test_agent_with_a_missing_provider_is_unavailable(client):
    user = owner(client)
    agent = run(client.app.state.agents.get(user.id, "default"))
    record = agent.to_record()
    broken = {**record.spec, "tts": {"provider": "removed-from-yaml"}}
    run(client.app.state.storage.agents.update(record.__class__(**{**record.__dict__, "spec": broken})))
    err = expect_fatal(client, pair(client), START, "agent_unavailable")
    assert "removed-from-yaml" not in err["message"]


def test_ws_binary_before_start_is_not_started(client):
    token = pair(client)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_bytes(silence(20))
        assert ws.receive_json()["code"] == "not_started"
        expect_close(ws, 4400)


def test_broken_provider_fails_at_startup():
    from wristcall.providers import ProviderError

    cfg = fake_config()
    data = cfg.model_dump(mode="json")
    data["providers"]["extra"] = {"type": "openai_stt"}
    with pytest.raises(ProviderError, match="extra"):
        create_app(parse_config(data, {}), storage=open_sqlite_storage(":memory:"))


# ---------- one-way calls (one-shot, monologue) ----------

import time  # noqa: E402

import httpx  # noqa: E402
import respx  # noqa: E402

from wristcall.delivery import DeliveryPolicy  # noqa: E402

HOOK_URL = "https://hooks.example/in"


RELAY = "https://relay.test"


def oneway_client(push=None, limits: dict | None = None):
    cfg = fake_config()
    if limits:
        cfg.limits = type(cfg.limits).model_validate({**cfg.limits.model_dump(), **limits})
    cfg.limits.custom_endpoint_types.append("fake_stt")
    cfg.limits.max_one_way_call_s = 60  # the lowest allowed
    cfg.push = push
    app = create_app(cfg, storage=open_sqlite_storage(":memory:"), delivery_policy=DeliveryPolicy(1.0, (0.0, 0.0)))
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        hook = mock.post(HOOK_URL).mock(return_value=httpx.Response(204))
        relay = mock.post(f"{RELAY}/v1/push/send").mock(return_value=httpx.Response(202, json={"status": "sent"}))
        with TestClient(app) as c:
            c.hook = hook
            c.relay = relay
            for slug, call_type in (("note", "one-shot"), ("ideas", "monologue")):
                run(c.app.state.agents.create(owner(c).id, {
                    "slug": slug, "call_type": call_type,
                    "stt": {"type": "fake_stt", "text": "comprar leite"},
                    "action": {"type": "webhook", "url": HOOK_URL, "headers": {"Authorization": "Bearer s3cret"}},
                    "vad": {"type": "energy"},
                }))
            yield c


@pytest.fixture
def oneway():
    yield from oneway_client()


def wait_done(client, call_id: str, headers: dict) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        view = client.get(f"/v1/calls/{call_id}", headers=headers).json()
        if view["status"] not in ("recording", "processing"):
            return view
        time.sleep(0.02)
    raise AssertionError(f"call {call_id} still {view['status']}")


def test_one_shot_call_records_until_hang_up_and_delivers(oneway):
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "note", "turn_end": "auto"})
        ready = ws.receive_json()
        assert ready["agent"]["call_type"] == "one-shot" and ready["turn_end"] == "manual"
        assert ready["profile"] == {"name": "note", "display_name": "note"}
        assert ready["audio_out"] == {"codec": "pcm16", "sample_rate": 16000, "channels": 1}
        call_id = ready["call_id"]
        # Long silence after speech ends nothing: no turn.user_end, no answer.
        ws.send_bytes(tone(500) + silence(2000))
        ws.send_json({"type": "mute", "muted": True})
        ws.send_json({"type": "session.end"})
    view = wait_done(oneway, call_id, auth(token))
    assert {k: view[k] for k in ("status", "text", "attempts", "last_http_status", "error", "call_type")} == {
        "status": "delivered", "text": "comprar leite", "attempts": 1, "last_http_status": 204, "error": None,
        "call_type": "one-shot",
    }
    assert view["agent"] == {"id": view["agent_id"], "slug": "note", "display_name": "note"}
    assert view["delivery"] == "webhook"
    assert [(e["role"], e["text"], e["error"]) for e in view["entries"]] == [("user", "comprar leite", None)]
    assert view["expires_at"] is None
    sent = oneway.hook.calls.last.request
    assert sent.headers["authorization"] == "Bearer s3cret" and sent.headers["idempotency-key"] == call_id
    body = json.loads(sent.content)
    assert (body["event"], body["call_id"], body["text"], body["agent"]["slug"]) == ("call.completed", call_id, "comprar leite", "note")


def test_one_way_call_is_delivered_after_a_dropped_connection(oneway):
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "ideas"})
        call_id = ws.receive_json()["call_id"]
        ws.send_bytes(tone(500))
    assert wait_done(oneway, call_id, auth(token))["status"] == "delivered"


def test_one_way_call_stops_at_the_limit(oneway):
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "ideas"})
        call_id = ws.receive_json()["call_id"]
        ws.send_bytes(tone(1000) + silence(59_000) + silence(1000))
        assert ws.receive_json() == {"type": "call.captured", "call_id": call_id, "reason": "limit"}
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1000
    assert wait_done(oneway, call_id, auth(token))["status"] == "delivered"


def test_failed_delivery_keeps_the_text(oneway):
    oneway.hook.mock(return_value=httpx.Response(500))
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "note"})
        call_id = ws.receive_json()["call_id"]
        ws.send_bytes(tone(500))
        ws.send_json({"type": "session.end"})
    view = wait_done(oneway, call_id, auth(token))
    assert (view["status"], view["error"], view["text"], view["attempts"], view["last_http_status"]) == (
        "failed", "delivery_failed", "comprar leite", 3, 500
    )


def test_silent_one_way_call_is_empty(oneway):
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "note"})
        call_id = ws.receive_json()["call_id"]
        ws.send_bytes(silence(1000))
        ws.send_json({"type": "session.end"})
    assert wait_done(oneway, call_id, auth(token))["status"] == "empty"
    assert not oneway.hook.called


def test_logging_setup_keeps_webhook_urls_out_of_the_log(oneway, monkeypatch, caplog):
    from wristcall.cli import configure_logging

    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", httpx_logger.level)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    monkeypatch.setattr(root, "level", root.level)
    configure_logging()
    # httpx logs "HTTP Request: POST <url>" at INFO; webhook URLs hold secrets in the path.
    assert httpx_logger.getEffectiveLevel() >= logging.WARNING
    caplog.set_level(logging.INFO)
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "note"})
        call_id = ws.receive_json()["call_id"]
        ws.send_bytes(tone(500))
        ws.send_json({"type": "session.end"})
    assert wait_done(oneway, call_id, auth(token))["status"] == "delivered"
    for secret in (HOOK_URL, "s3cret", "comprar leite"):
        assert secret not in caplog.text


def test_call_status_is_private_to_the_user(oneway):
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "note"})
        call_id = ws.receive_json()["call_id"]
        ws.send_json({"type": "session.end"})
    wait_done(oneway, call_id, auth(token))
    # The owner's API token sees it too.
    _, api_token = run(UserService(oneway.app.state.storage).issue_token(owner(oneway).id, "cli"))
    assert oneway.get(f"/v1/calls/{call_id}", headers=auth(api_token)).status_code == 200
    other = run(oneway.app.state.storage.users.create("u_other", "other", "Other", 1.0))
    assert oneway.get(f"/v1/calls/{call_id}", headers=auth(pair(oneway, other.id))).status_code == 404
    assert oneway.get(f"/v1/calls/{call_id}").status_code == 401
    assert oneway.get("/v1/calls/c_nope", headers=auth(token)).status_code == 404


def test_client_gone_before_session_ready_still_finishes_the_call(oneway, monkeypatch):
    from wristcall import app as app_module

    async def gone(self, msg):
        raise RuntimeError("client gone")

    monkeypatch.setattr(app_module._WsTransport, "send_json", gone)
    token = pair(oneway)
    with pytest.raises(Exception):
        with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
            ws.send_json({**START, "agent": "note"})
            ws.receive_json()
    [row] = oneway.app.state.storage.db.query("SELECT id FROM calls")
    monkeypatch.undo()
    assert wait_done(oneway, row["id"], auth(token))["status"] == "empty"


def test_conversation_is_recorded_in_the_history(client):
    token = pair(client)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json(START)
        call_id = ws.receive_json()["call_id"]
        ws.send_bytes(tone(500) + silence(900))
        read_until_agent_end(ws)
        ws.send_json({"type": "session.end"})
    view = client.get(f"/v1/calls/{call_id}", headers=auth(token)).json()
    assert (view["call_type"], view["status"], view["error"]) == ("conversation", "ended", None)
    assert view["delivery"] is None
    assert view["agent"]["slug"] == "default" and view["ended_at"] >= view["created_at"]
    [user, agent] = view["entries"]
    assert (user["role"], agent["role"], agent["error"]) == ("user", "agent", None)
    assert agent["text"] == f"You said: {user['text']}" and view["text"] == user["text"]


def test_conversation_without_speech_is_empty(client):
    token = pair(client)
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json(START)
        call_id = ws.receive_json()["call_id"]
        ws.send_json({"type": "session.end"})
    view = client.get(f"/v1/calls/{call_id}", headers=auth(token)).json()
    assert (view["status"], view["entries"], view["text"]) == ("empty", [], None)


def test_server_refuses_to_start_with_another_history_key():
    from wristcall.history_codec import HistoryKeyError, new_key

    store = open_sqlite_storage(":memory:")
    cfg = fake_config()
    cfg.history.encryption_key = new_key()
    with TestClient(create_app(cfg, storage=store)):
        pass
    cfg.history.encryption_key = new_key()
    with pytest.raises(HistoryKeyError, match="not the key"):
        with TestClient(create_app(cfg, storage=store)):
            pass


def test_expired_calls_are_purged_at_startup():
    from wristcall.storage import CallRecord

    store = open_sqlite_storage(":memory:")
    cfg = fake_config()
    with TestClient(create_app(cfg, storage=store)) as c:
        user = owner(c)
    run(store.calls.create(CallRecord(
        id="c_old", user_id=user.id, agent_id="ag_gone", device_id=None, call_type="one-shot",
        status="delivered", created_at=1.0, updated_at=1.0, expires_at=2.0,
    )))
    with TestClient(create_app(cfg, storage=store)):
        pass
    assert run(store.calls.get(user.id, "c_old")) is None


def test_unfinished_calls_are_interrupted_at_startup():
    store = open_sqlite_storage(":memory:")
    cfg = fake_config()
    with TestClient(create_app(cfg, storage=store)) as c:
        user = owner(c)
        agent = run(c.app.state.agents.list(user.id))[0]
    from wristcall.storage import CallRecord, EntryRecord
    run(store.calls.create(CallRecord(
        id="c_old", user_id=user.id, agent_id=agent.id, device_id=None, call_type="one-shot",
        status="processing", created_at=1.0, updated_at=1.0,
    )))
    run(store.calls.add_entry(user.id, EntryRecord("c_old", 0, "user", "half", False, None, 1.0), ["half"]))
    with TestClient(create_app(cfg, storage=store)):
        pass
    old = run(store.calls.get(user.id, "c_old"))
    assert (old.status, old.error) == ("failed", "interrupted")
    assert [e.text for e in run(store.calls.entries(user.id, "c_old"))] == ["half"]


def test_watch_0_1_0_calling_a_one_way_first_agent_records_and_ends_normally(oneway):
    run(oneway.app.state.agents.update(owner(oneway).id, "note", {"position": 0}))
    token = pair(oneway)
    me = oneway.get("/v1/me", headers=auth(token)).json()
    assert me["profiles"][0]["name"] == "note"
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        # What watch 0.1.0 sends: the first profile, no agent, no turn_end.
        ws.send_json({**START, "profile": "note"})
        ready = ws.receive_json()
        assert {"session_id", "profile", "audio_out"} <= ready.keys()
        ws.send_bytes(tone(500) + silence(3000))
        ws.send_json({"type": "session.end"})
    assert wait_done(oneway, ready["call_id"], auth(token))["status"] == "delivered"


def test_one_way_call_end_sends_push():
    from wristcall.config import PushConfig

    key = "wc_push_" + "k" * 43
    for client in oneway_client(PushConfig(relay_url=RELAY, timeout_s=0.5)):
        token = pair(client)
        assert client.put("/v1/push", headers=auth(token), json={"push_key": key}).status_code == 204
        with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
            ws.send_json({**START, "agent": "note"})
            call_id = ws.receive_json()["call_id"]
            ws.send_bytes(tone(500))
            ws.send_json({"type": "session.end"})
        view = wait_done(client, call_id, auth(token))
        assert (view["status"], view["error"]) == ("delivered", None)
        deadline = time.monotonic() + 2
        while not client.relay.called and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.relay.call_count == 1
        req = client.relay.calls.last.request
        assert req.headers["authorization"] == f"Bearer {key}"
        assert json.loads(req.content) == {
            "event": "call.finished", "title": "Delivered", "body": "note got your message.",
            "data": {"call_id": call_id, "status": "delivered", "error": None, "agent_id": view["agent_id"]},
            "collapse_id": call_id,
        }


def test_one_way_call_end_without_push_config_sends_nothing(oneway):
    token = pair(oneway)
    with oneway.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "note"})
        call_id = ws.receive_json()["call_id"]
        ws.send_bytes(tone(500))
        ws.send_json({"type": "session.end"})
    assert wait_done(oneway, call_id, auth(token))["status"] == "delivered"
    time.sleep(0.05)
    assert not oneway.relay.called


HISTORY_AGENT = {"slug": "inbox", "display_name": "Inbox", "call_type": "one-shot",
                 "stt": {"type": "fake_stt", "text": "comprar leite"}, "vad": {"type": "energy"}}


def test_history_only_one_shot_call():
    from wristcall.config import PushConfig

    key = "wc_push_" + "k" * 43
    for c in oneway_client(PushConfig(relay_url=RELAY, timeout_s=0.5), limits={"history_only_one_way": True}):
        created = c.post("/v1/agents", headers=auth(api_token(c)), json=HISTORY_AGENT)
        assert created.status_code == 201 and created.json()["action"] is None
        token = pair(c)
        assert c.put("/v1/push", headers=auth(token), json={"push_key": key}).status_code == 204
        with c.websocket_connect("/v1/call", headers=auth(token)) as ws:
            ws.send_json({**START, "agent": "inbox"})
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            ws.send_bytes(tone(500))
            ws.send_json({"type": "session.end"})
        view = wait_done(c, ready["call_id"], auth(token))
        assert {k: view[k] for k in ("status", "delivery", "attempts", "last_http_status", "error", "text")} == {
            "status": "delivered", "delivery": "history", "attempts": 0, "last_http_status": None, "error": None,
            "text": "comprar leite",
        }
        deadline = time.monotonic() + 2
        while not c.relay.called and time.monotonic() < deadline:
            time.sleep(0.01)
        assert c.relay.call_count == 1
        message = json.loads(c.relay.calls.last.request.content)
        assert (message["title"], message["body"], message["data"]["status"]) == ("Saved", "Saved to Inbox.", "delivered")
        assert not c.hook.called


def test_history_agent_after_the_limit_is_turned_off():
    storage = open_sqlite_storage(":memory:")
    on, off = fake_config(), fake_config()
    on.limits.history_only_one_way = True
    for cfg in (on, off):
        cfg.limits.custom_endpoint_types.append("fake_stt")
    with TestClient(create_app(on, storage=storage)) as c:
        assert c.post("/v1/agents", headers=auth(api_token(c)), json=HISTORY_AGENT).status_code == 201
    with TestClient(create_app(off, storage=storage)) as c:
        expect_fatal(c, pair(c), {**START, "agent": "inbox"}, "agent_unavailable")
        token = api_token(c)
        assert "inbox" in [a["slug"] for a in c.get("/v1/agents", headers=auth(token)).json()["agents"]]
        assert c.get("/v1/agents/inbox", headers=auth(token)).json()["action"] is None
        assert c.get("/v1/calls", headers=auth(token)).json()["calls"] == []


def test_startup_logs_the_link_qr_on_a_new_server(caplog):
    from test_pair_account import account_config

    import re

    from wristcall.pairing import hash_secret

    # A new server: exactly one user, not linked, with no API token and no device.
    store = open_sqlite_storage(":memory:")
    ana = run(UserService(store).create("ana"))
    caplog.set_level(logging.INFO, logger="wristcall.link")
    with TestClient(create_app(account_config("attestation", public_url="https://testserver"), storage=store)):
        (user,) = run(store.users.list())
        assert user.id == ana.id and user.central_subject is None
        assert run(store.tokens.list(ana.id)) == [] and run(store.devices.list(ana.id)) == []
    (record,) = [r for r in caplog.records if r.name == "wristcall.link"]
    message = record.getMessage()
    assert "https://wristcall-app.trigram.com.br/link#v=1&s=https://testserver&c=" in message
    assert "New code: docker compose exec wristcall wristcall link" in message
    code = re.search(r"&c=([A-Z2-7]{26})", message).group(1)
    assert run(store.pairing.claim_link_code(hash_secret(code), time.time())) == ana.id


def test_startup_survives_a_failing_link_invite(monkeypatch, caplog):
    from test_pair_account import account_config

    async def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("wristcall.app.startup_invite", broken)
    caplog.set_level(logging.WARNING, logger="wristcall")
    with TestClient(create_app(account_config("attestation", public_url="https://testserver"), storage=open_sqlite_storage(":memory:"))) as c:
        assert c.get("/v1/health").status_code == 200
    assert any(
        r.name == "wristcall.app" and r.levelno == logging.WARNING and "could not prepare the link QR code" in r.getMessage()
        for r in caplog.records
    )


def test_agent_without_color_reports_null(client):
    token = pair(client)
    (agent,) = client.get("/v1/me", headers=auth(token)).json()["agents"]
    assert "color" in agent and agent["color"] is None
    run(client.app.state.agents.create(owner(client).id, {"slug": "tinted", "color": "pink"}))
    colors = {a["slug"]: a["color"] for a in client.get("/v1/me", headers=auth(token)).json()["agents"]}
    assert colors == {"default": None, "tinted": "pink"}


import httpcore  # noqa: E402

from conftest import fake_resolver  # noqa: E402
from wristcall.agents import AgentService  # noqa: E402

PUBLIC_IP, PRIVATE_IP = "93.184.216.34", "10.0.0.9"


def public_only_client(resolver, *, public_only=True, hook_route=True, tts=None):
    """A one-shot agent `note` written straight to the storage (no check when saved), webhook at HOOK_URL."""
    cfg = fake_config()
    cfg.limits.custom_endpoint_types.append("fake_stt")
    cfg.limits.public_only = public_only
    app = create_app(
        cfg, storage=open_sqlite_storage(":memory:"), delivery_policy=DeliveryPolicy(1.0, (0.0, 0.0)), resolver=resolver
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        if hook_route:
            hook = mock.post(HOOK_URL).mock(return_value=httpx.Response(204))
        else:
            # The request goes on to the real transport, so the checking backend runs.
            hook = mock.route(host="hooks.example").pass_through()
        with TestClient(app) as c:
            c.hook = hook
            plain = fake_config()
            plain.limits.custom_endpoint_types.append("fake_stt")
            run(AgentService(c.app.state.storage, plain, httpx.AsyncClient()).create(owner(c).id, {
                "slug": "note", "call_type": "one-shot",
                "stt": {"type": "fake_stt", "text": "comprar leite"},
                "action": {"type": "webhook", "url": HOOK_URL},
                "vad": {"type": "energy"},
                **({"tts": tts} if tts else {}),
            }))
            yield c


def api_token(client) -> str:
    return run(UserService(client.app.state.storage).issue_token(owner(client).id, "cli"))[1]


def one_shot(client, token: str) -> str:
    with client.websocket_connect("/v1/call", headers=auth(token)) as ws:
        ws.send_json({**START, "agent": "note"})
        ready = ws.receive_json()
        assert ready["type"] == "session.ready"
        ws.send_bytes(tone(500))
        ws.send_json({"type": "session.end"})
    return ready["call_id"]


def test_public_only_refuses_a_call_to_an_internal_address():
    for c in public_only_client(fake_resolver({"hooks.example": [PRIVATE_IP]})):
        token = pair(c)
        err = expect_fatal(c, token, {**START, "agent": "note"}, "agent_unavailable")
        assert err["reason"] == "url_not_public"
        assert err["message"] == "This agent uses an address this server cannot call."
        assert c.get("/v1/calls", headers=auth(api_token(c))).json()["calls"] == []
        assert not c.hook.called


def test_session_start_dns_failure_says_unresolved():
    for c in public_only_client(fake_resolver({})):  # the resolver raises OSError for hooks.example
        token = pair(c)
        err = expect_fatal(c, token, {**START, "agent": "note"}, "agent_unavailable")
        assert err == {
            "type": "error", "code": "agent_unavailable", "fatal": True,
            "message": "Can't look up this agent's address right now; try again.", "reason": "url_unresolved",
        }
        assert c.get("/v1/calls", headers=auth(api_token(c))).json()["calls"] == []
        assert not c.hook.called


def test_public_only_lets_a_public_agent_call():
    for c in public_only_client(fake_resolver({"hooks.example": [PUBLIC_IP]})):
        token = pair(c)
        view = wait_done(c, one_shot(c, token), auth(token))
        assert (view["status"], view["attempts"], view["last_http_status"]) == ("delivered", 1, 204)
        assert c.hook.called


def test_public_only_ignores_the_stored_voice_of_a_one_way_agent():
    # The voice is kept for a switch back to conversation; a one-shot call never uses it, so it must not refuse the call.
    tts = {"type": "openai_tts", "base_url": "https://speaches:8000/v1", "model": "t", "voice": "v"}
    for c in public_only_client(fake_resolver({"hooks.example": [PUBLIC_IP]}), tts=tts):
        token = pair(c)
        view = wait_done(c, one_shot(c, token), auth(token))
        assert (view["status"], view["last_http_status"]) == ("delivered", 204)


def test_without_public_only_the_resolver_is_never_used():
    async def resolver(host: str, port: int) -> list[str]:
        raise AssertionError(f"resolver used for {host}")

    for c in public_only_client(resolver, public_only=False):
        token = pair(c)
        view = wait_done(c, one_shot(c, token), auth(token))
        assert (view["status"], view["last_http_status"]) == ("delivered", 204)


def test_public_only_rebinding_after_start_fails_delivery(monkeypatch):
    asked: list[str] = []

    async def rebinding(host: str, port: int) -> list[str]:
        # Public for the check at session.start, private when the delivery opens its connection.
        asked.append(host)
        return [PUBLIC_IP] if len(asked) == 1 else [PRIVATE_IP]

    connected: list[str] = []

    async def no_network(self, host, port, **kwargs):
        connected.append(host)
        raise httpcore.ConnectError("no network in tests")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", no_network)
    for c in public_only_client(rebinding, hook_route=False):
        token = pair(c)
        view = wait_done(c, one_shot(c, token), auth(token))
        assert (view["status"], view["error"], view["attempts"], view["last_http_status"]) == (
            "failed", "delivery_failed", 1, None
        )
        # The second lookup is the checking backend's, at connect time; nothing reached the network.
        assert asked == ["hooks.example", "hooks.example"] and connected == []
        # respx records a pass-through request as it goes by; none of them got an answer.
        assert c.hook.called and not any(call.has_response for call in c.hook.calls)


# ---------- limits.max_concurrent_calls ----------

import threading  # noqa: E402

END = {"type": "session.end"}
BUSY = "This server is busy; try again in a minute."


def wait_until(check, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def limited(**limits):
    return oneway_client(limits={"max_concurrent_calls": limits})


def test_no_limit_by_default(client):
    token = pair(client)
    with client.websocket_connect("/v1/call", headers=auth(token)) as a, \
            client.websocket_connect("/v1/call", headers=auth(token)) as b:
        for ws in (a, b):
            ws.send_json(START)
            assert ws.receive_json()["type"] == "session.ready"
        assert client.app.state.call_slots.active() == {"conversation": 2, "one_way": 0}
        a.send_json(END)
        b.send_json(END)


def test_busy_when_conversations_are_at_the_limit():
    for c in limited(conversation=1):
        token = pair(c)
        with c.websocket_connect("/v1/call", headers=auth(token)) as first:
            first.send_json(START)
            assert first.receive_json()["type"] == "session.ready"
            err = expect_fatal(c, token, START, "busy")
            assert err["message"] == BUSY and "reason" not in err
            first.send_json(END)
            expect_close(first, 1000)
        wait_until(lambda: c.app.state.call_slots.active()["conversation"] == 0)
        with c.websocket_connect("/v1/call", headers=auth(token)) as third:
            third.send_json(START)
            assert third.receive_json()["type"] == "session.ready"
            third.send_json(END)
        # The refused call left no record.
        assert len(c.app.state.storage.db.query("SELECT id FROM calls")) == 2


def test_one_way_limit_is_separate():
    for c in limited(one_way=1):
        token = pair(c)
        with c.websocket_connect("/v1/call", headers=auth(token)) as note:
            note.send_json({**START, "agent": "note"})
            assert note.receive_json()["type"] == "session.ready"
            with c.websocket_connect("/v1/call", headers=auth(token)) as talk:
                talk.send_json(START)
                assert talk.receive_json()["type"] == "session.ready"
                talk.send_json(END)
            err = expect_fatal(c, token, {**START, "agent": "ideas"}, "busy")
            assert err["message"] == BUSY
            note.send_json(END)


def test_slot_is_released_when_the_client_drops():
    for c in limited(conversation=1):
        token = pair(c)
        with c.websocket_connect("/v1/call", headers=auth(token)) as ws:
            ws.send_json(START)
            assert ws.receive_json()["type"] == "session.ready"
        # No session.end: the client just went away.
        wait_until(lambda: c.app.state.call_slots.active()["conversation"] == 0)
        with c.websocket_connect("/v1/call", headers=auth(token)) as ws:
            ws.send_json(START)
            assert ws.receive_json()["type"] == "session.ready"
            ws.send_json(END)


@pytest.mark.parametrize("agent", ["default", "note"])
def test_slot_is_released_when_the_call_fails_to_start(monkeypatch, agent):
    for c in limited(conversation=1, one_way=1):
        token = pair(c)

        async def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(c.app.state.storage.calls, "create", broken)
        with pytest.raises(RuntimeError):
            with c.websocket_connect("/v1/call", headers=auth(token)) as ws:
                ws.send_json({**START, "agent": agent})
                ws.receive_json()
        wait_until(lambda: c.app.state.call_slots.active() == {"conversation": 0, "one_way": 0})
        monkeypatch.undo()
        assert c.app.state.storage.db.query("SELECT id FROM calls") == []


def test_one_shot_holds_the_slot_until_transcribed(monkeypatch):
    from wristcall.providers.fake import FakeStt

    started, gate = threading.Event(), threading.Event()

    async def gated(self, wav: bytes, language: str) -> str:
        started.set()
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return self.text

    monkeypatch.setattr(FakeStt, "transcribe", gated)
    for c in limited(one_way=1):
        token = pair(c)
        with c.websocket_connect("/v1/call", headers=auth(token)) as ws:
            ws.send_json({**START, "agent": "note"})
            call_id = ws.receive_json()["call_id"]
            ws.send_bytes(tone(500))
            ws.send_json(END)
        # One-shot transcribes after hang-up: the WebSocket is gone but the call still holds its slot.
        assert started.wait(5)
        assert c.app.state.call_slots.active()["one_way"] == 1
        expect_fatal(c, token, {**START, "agent": "note"}, "busy")
        gate.set()
        assert wait_done(c, call_id, auth(token))["status"] == "delivered"
        wait_until(lambda: c.app.state.call_slots.active()["one_way"] == 0)
