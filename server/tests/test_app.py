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
    assert client.get("/v1/health").json() == {"status": "ok", "version": __version__, "protocol": 1, "account": None}


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
    assert set(agent) == {"id", "slug", "display_name", "icon", "call_type", "turn_end"}
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


@pytest.fixture
def oneway():
    cfg = fake_config()
    cfg.limits.custom_endpoint_types.append("fake_stt")
    cfg.limits.max_one_way_call_s = 60  # the lowest allowed
    app = create_app(cfg, storage=open_sqlite_storage(":memory:"), delivery_policy=DeliveryPolicy(1.0, (0.0, 0.0)))
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        hook = mock.post(HOOK_URL).mock(return_value=httpx.Response(204))
        with TestClient(app) as c:
            c.hook = hook
            for slug, call_type in (("note", "one-shot"), ("ideas", "monologue")):
                run(c.app.state.agents.create(owner(c).id, {
                    "slug": slug, "call_type": call_type,
                    "stt": {"type": "fake_stt", "text": "comprar leite"},
                    "action": {"type": "webhook", "url": HOOK_URL, "headers": {"Authorization": "Bearer s3cret"}},
                    "vad": {"type": "energy"},
                }))
            yield c


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
