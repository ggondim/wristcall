import asyncio
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from conftest import fake_config
from wristcall.app import create_app
from wristcall.delivery import DeliveryPolicy
from wristcall.history import CallLog
from wristcall.storage import CallRecord, open_sqlite_storage
from wristcall.users import UserService

HOOK_URL = "https://hooks.example/in"


def run(coro):
    return asyncio.run(coro)


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app():
    cfg = fake_config()
    application = create_app(
        cfg, storage=open_sqlite_storage(":memory:"), delivery_policy=DeliveryPolicy(1.0, (0.0, 0.0))
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        hook = mock.post(HOOK_URL).mock(return_value=httpx.Response(204))
        with TestClient(application) as c:
            c.hook = hook
            st = c.app.state.storage
            c.owner = run(st.users.by_handle("owner"))
            c.token = run(UserService(st).issue_token(c.owner.id, "test"))[1]
            c.note = run(c.app.state.agents.create(c.owner.id, {
                "slug": "note", "call_type": "one-shot", "language": "pt",
                "action": {"type": "webhook", "url": HOOK_URL},
            }))
            yield c


def failed_call(c, call_id="c_1", *, error="delivery_failed", text="comprar leite", agent_id=None, call_type="one-shot",
                status="failed", attempts=3):
    history = c.app.state.history
    record = run(history.storage.calls.create(CallRecord(
        id=call_id, user_id=c.owner.id, agent_id=agent_id or c.note.id, device_id=None, call_type=call_type,
        status=status, error=error, attempts=attempts, last_http_status=500, created_at=100.0, updated_at=100.0,
        ended_at=110.0, finished_at=160.0, agent_slug="note", agent_name="note",
    )))
    if text:
        run(CallLog(history.storage, history.codec, record).add("user", text))
    return record


def wait_done(c, call_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        view = c.get(f"/v1/calls/{call_id}", headers=h(c.token)).json()
        if view["status"] != "processing":
            return view
        time.sleep(0.02)
    raise AssertionError("still processing")


def test_a_failed_delivery_is_sent_again_with_the_same_key(app):
    failed_call(app)
    r = app.post("/v1/calls/c_1/redeliver", headers=h(app.token))
    assert r.status_code == 202 and r.json()["status"] == "processing" and r.json()["error"] is None
    view = wait_done(app, "c_1")
    assert (view["status"], view["error"], view["attempts"], view["last_http_status"]) == ("delivered", None, 4, 204)
    sent = app.hook.calls.last.request
    assert sent.headers["idempotency-key"] == "c_1"
    body = json.loads(sent.content)
    assert (body["call_id"], body["text"], body["language"], body["agent"]["slug"]) == ("c_1", "comprar leite", "pt", "note")
    assert body["started_at"] == "1970-01-01T00:01:40Z" and body["ended_at"] == "1970-01-01T00:01:50Z"


def test_an_interrupted_call_with_text_can_be_redelivered(app):
    failed_call(app, error="interrupted", attempts=0)
    assert app.post("/v1/calls/c_1/redeliver", headers=h(app.token)).status_code == 202
    assert wait_done(app, "c_1")["status"] == "delivered"


def test_failing_again_adds_the_attempts(app):
    app.hook.mock(return_value=httpx.Response(503))
    failed_call(app)
    app.post("/v1/calls/c_1/redeliver", headers=h(app.token))
    view = wait_done(app, "c_1")
    assert (view["status"], view["error"], view["attempts"], view["last_http_status"]) == ("failed", "delivery_failed", 6, 503)


@pytest.mark.parametrize("setup, code", [
    (dict(status="delivered", error=None), "not_failed"),
    (dict(error="stt_failed"), "not_failed"),
    (dict(status="processing", error=None), "busy"),
    (dict(text=None), "no_text"),
    (dict(call_type="conversation", status="ended", error=None), "not_one_way"),
    (dict(agent_id="ag_0123456789ab"), "agent_gone"),
])
def test_what_cannot_be_redelivered(app, setup, code):
    failed_call(app, **setup)
    r = app.post("/v1/calls/c_1/redeliver", headers=h(app.token))
    assert r.status_code == 409 and r.json()["error"] == code
    assert app.hook.calls.call_count == 0


def test_an_agent_turned_into_a_conversation_is_not_redelivered(app):
    failed_call(app)
    run(app.app.state.agents.update(app.owner.id, "note", {"call_type": "conversation", "action": {"provider": "llm"}, "tts": {"provider": "tts"}}))
    r = app.post("/v1/calls/c_1/redeliver", headers=h(app.token))
    assert r.status_code == 409 and r.json()["error"] == "not_one_way"


def test_the_agent_webhook_as_it_is_now_is_used(app):
    failed_call(app)
    run(app.app.state.agents.update(app.owner.id, "note", {"action": {"type": "webhook", "url": HOOK_URL + "/v2"}}))
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        v2 = mock.post(HOOK_URL + "/v2").mock(return_value=httpx.Response(200))
        app.post("/v1/calls/c_1/redeliver", headers=h(app.token))
        assert wait_done(app, "c_1")["status"] == "delivered"
        assert v2.called


def test_unknown_or_foreign_call_is_404(app):
    assert app.post("/v1/calls/c_nope/redeliver", headers=h(app.token)).status_code == 404
