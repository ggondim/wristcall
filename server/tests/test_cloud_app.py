"""Cloud mode (`server.mode: cloud`): the node assembled in create_app and the management API through the Cloud."""

import asyncio
import json
import time

import httpx
import pytest
from cloud_app_fixtures import NODE_SECRET, bearer, cloud_app, cloud_config, new_fake, wait_ready
from conftest import fake_config
from fake_cloud import FULL_PLAN, FakeCloud
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wristcall.admission import CallSlots
from wristcall.app import create_app
from wristcall.cloud_node.node import CloudNode
from wristcall.cloud_node.outbox import OutboxItem
from wristcall.cloud_node.proof import ProofMismatch, UserProof, current_proof
from wristcall.cloud_node.seal import SealCodec
from wristcall.history import aad_scoped
from wristcall.history_codec import HistoryCodec
from wristcall.storage import (
    AgentRecord, CallOpen, CloudUnavailable, DeviceRevoked, NotSupported, PlanRequired, open_sqlite_storage,
)

ACCOUNT = "acc-1"
CUSTOM_PLAN = {"plan": "custom", "source": "beta", "expires_at": None}
LLM = {"type": "openai_chat", "base_url": "https://llm.example.test/v1", "model": "m", "api_key": "sk-agent-secret"}
OWN_STT = {"type": "openai_stt", "base_url": "https://stt.example.test/v1", "model": "w", "api_key": "sk-stt"}
PUSH_KEY = "wc_push_" + "k" * 43
NOTES = {"slug": "notes", "call_type": "one-shot"}

NOT_IN_CLOUD = {"error": "not_in_cloud_mode", "message": "Not available on a Wristcall Cloud node."}
CLOUD_DOWN = {"error": "cloud_unavailable", "message": "Wristcall Cloud is unavailable; try again."}
NO_PLAN = {"error": "plan_required", "message": "This account has no Wristcall Cloud plan."}
REVOKED = {"error": "device_revoked", "message": "This device was removed from the account."}


@pytest.fixture
def fake() -> FakeCloud:
    cloud = new_fake()
    cloud.plans[ACCOUNT] = FULL_PLAN
    return cloud


def ios(fake: FakeCloud, account: str = ACCOUNT) -> dict[str, str]:
    return bearer(fake, account, "dev-ios", "ios")


def watch(fake: FakeCloud, account: str = ACCOUNT) -> dict[str, str]:
    return bearer(fake, account, "dev-watch", "watchos")


# --- health and startup ------------------------------------------------------------------------------------------


def test_self_hosted_health_unchanged():
    app = create_app(fake_config(), storage=open_sqlite_storage(":memory:"))
    assert app.state.cloud_node is None
    with TestClient(app) as c:
        r = c.get("/v1/health")
    assert r.status_code == 200
    assert set(r.json()) == {"status", "version", "protocol", "account", "push"}
    assert r.json()["status"] == "ok"


def test_health_starting_then_ready(fake: FakeCloud):
    fake.down = True  # no heartbeat accepted yet: deterministic "starting"
    app = cloud_app(fake)
    assert isinstance(app.state.cloud_node, CloudNode)
    with TestClient(app) as c:
        r = c.get("/v1/health")
        assert r.status_code == 503
        assert r.json()["status"] == "starting" and r.json()["mode"] == "cloud"
        fake.down = False
        body = wait_ready(c)
        assert body["status"] == "ok" and body["mode"] == "cloud"
        assert {"version", "protocol", "account", "push"} <= set(body)
        assert fake.heartbeats  # the beat the node waited for
        app.state.cloud_node.draining = True
        r = c.get("/v1/health")
        assert (r.status_code, r.json()["status"]) == (503, "draining")
        app.state.cloud_node.draining = False


def test_leaving_with_the_cloud_down_is_quick(fake: FakeCloud):
    # I13: the shutdown waits are injectable, so a test that ends with the Cloud down does not wait ~28 s.
    app = cloud_app(fake)
    with TestClient(app) as c:
        wait_ready(c)
        fake.down = True
        app.state.cloud_node.outbox.put(OutboxItem("c_0", "patch", "/v1/node/calls/c_0", {"status": "failed"}, "g"))
        started = time.monotonic()
    assert time.monotonic() - started < 2.0


# --- routes that do not exist in cloud mode ----------------------------------------------------------------------


@pytest.mark.parametrize("method, path", [
    ("POST", "/v1/pair"),
    ("POST", "/v1/pair/account"),
    ("POST", "/v1/pair/poll"),
    ("POST", "/v1/pairing-codes"),
    ("GET", "/v1/pairing-requests"),
    ("POST", "/v1/pairing-requests/r1/approve"),
    ("POST", "/v1/pairing-requests/r1/deny"),
    ("POST", "/v1/account/link"),
    ("DELETE", "/v1/account/link"),
    ("GET", "/v1/devices"),
    ("DELETE", "/v1/devices/d1"),
    ("DELETE", "/v1/me"),
])
def test_cloud_only_routes_are_404(fake: FakeCloud, method, path):
    with TestClient(cloud_app(fake)) as c:
        r = c.request(method, path, headers=ios(fake), json={})
    assert r.status_code == 404
    assert r.json() == NOT_IN_CLOUD


def test_self_hosted_keeps_those_routes():
    with TestClient(create_app(fake_config(), storage=open_sqlite_storage(":memory:"))) as c:
        assert c.get("/v1/devices").json()["error"] == "unauthorized"
        assert c.delete("/v1/me").json()["error"] == "unauthorized"


def test_websocket_call_refused_until_task_9(fake: FakeCloud):
    with TestClient(cloud_app(fake)) as c, c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401


# --- error mapping -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("error, status, body", [
    (CloudUnavailable("x"), 503, CLOUD_DOWN),
    (PlanRequired("x"), 402, NO_PLAN),
    (DeviceRevoked("x"), 403, REVOKED),
    (NotSupported("x"), 404, NOT_IN_CLOUD),
    (CallOpen("x"), 409, {"error": "call_open", "message": "This call is still open."}),
    (ProofMismatch("x"), 401, {"error": "unauthorized", "message": "missing or invalid token"}),
])
def test_exception_handlers(fake: FakeCloud, error, status, body):
    app = cloud_app(fake)

    @app.get("/test-raise")
    async def boom():
        raise error

    with TestClient(app) as c:
        r = c.get("/test-raise")
    assert (r.status_code, r.json()) == (status, body)


def test_other_permission_errors_are_not_401(fake: FakeCloud):
    app = cloud_app(fake)

    @app.get("/test-raise")
    async def boom():
        raise PermissionError("not the adapter's")

    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/test-raise").status_code == 500


@pytest.mark.parametrize("case", ["down", "revoked", "other_node", "no_jti"])
def test_errors_map(fake: FakeCloud, case):
    with TestClient(cloud_app(fake)) as c:
        wait_ready(c)
        assert c.get("/v1/calls", headers=ios(fake)).status_code == 200  # the Cloud's keys are fetched and kept
        headers = ios(fake)
        if case == "down":
            fake.down = True
            expected = (503, CLOUD_DOWN)
        elif case == "revoked":
            fake.revoked.add("dev-ios")
            expected = (403, REVOKED)
        else:
            claims = {"aud": "https://other.test"} if case == "other_node" else {"jti": None}
            headers = bearer(fake, ACCOUNT, "dev-ios", "ios", **claims)
            expected = (401, {"error": "unauthorized", "message": "missing or invalid token"})
        for path in ("/v1/calls", "/v1/me"):
            r = c.get(path, headers=headers)
            assert (r.status_code, r.json()) == expected, path


# --- agents ------------------------------------------------------------------------------------------------------


def seal_codec() -> SealCodec:
    return SealCodec(cloud_config().cloud_node.seal())


def test_agents_crud_through_cloud(fake: FakeCloud):
    with TestClient(cloud_app(fake)) as c:
        r = c.post("/v1/agents", json={"slug": "coach", "action": LLM, "system_prompt": "Be brief."}, headers=ios(fake))
        assert r.status_code == 201, r.text
        coach = r.json()
        assert coach["action"]["api_key"] == "***"
        stored = fake.agents[coach["id"]]
        assert set(stored["spec"]["action"]) == {"sealed"} and stored["spec"]["action"]["sealed"].startswith("v1.")
        assert stored["spec"]["stt"] == {"provider": "stt"} and stored["spec"]["tts"] == {"provider": "tts"}
        assert "sk-agent-secret" not in json.dumps(fake.agents)

        r = c.post("/v1/agents", json={"slug": "notes", "call_type": "one-shot"}, headers=ios(fake))
        assert r.status_code == 201, r.text
        notes = r.json()
        assert notes["action"] is None  # limits.history_only_one_way: the calls go to the history

        listed = c.get("/v1/agents", headers=ios(fake)).json()["agents"]
        assert [a["slug"] for a in listed] == ["coach", "notes"]
        # Reorder: notes first.
        assert c.patch("/v1/agents/notes", json={"position": 0}, headers=ios(fake)).status_code == 200
        assert [a["slug"] for a in c.get("/v1/agents", headers=ios(fake)).json()["agents"]] == ["notes", "coach"]

        # Edit: `***` keeps the stored secret.
        r = c.patch("/v1/agents/coach", json={"display_name": "Coach 2", "action": {**LLM, "api_key": "***"}},
                    headers=ios(fake))
        assert r.status_code == 200, r.text
        assert r.json()["display_name"] == "Coach 2"
        opened = seal_codec().open_spec(fake.agents[coach["id"]]["spec"], ACCOUNT, coach["id"])
        assert opened["action"]["api_key"] == "sk-agent-secret"
        assert c.get("/v1/agents/coach", headers=ios(fake)).json()["display_name"] == "Coach 2"

        # The watch lists summaries and cannot write.
        seen = c.get("/v1/agents", headers=watch(fake)).json()["agents"]
        assert [a["slug"] for a in seen] == ["notes", "coach"]
        assert "action" not in seen[0] and "stt" not in seen[0]
        r = c.post("/v1/agents", json={"slug": "nope"}, headers=watch(fake))
        assert (r.status_code, r.json()["error"]) == (403, "forbidden")

        assert c.delete("/v1/agents/coach", headers=ios(fake)).status_code == 204
        assert coach["id"] not in fake.agents
        assert [a["slug"] for a in c.get("/v1/agents", headers=ios(fake)).json()["agents"]] == ["notes"]


def test_patch_retention_days_cloud_mode(fake: FakeCloud):
    # B2: the Cloud moves the calls' expiry itself; the node's set_expiry is a no-op, never a 500.
    with TestClient(cloud_app(fake)) as c:
        assert c.post("/v1/agents", json=NOTES, headers=ios(fake)).status_code == 201
        r = c.patch("/v1/agents/notes", json={"retention_days": 7}, headers=ios(fake))
        assert r.status_code == 200, r.text
        assert r.json()["retention_days"] == 7


def test_agent_the_cloud_refuses_is_422(fake: FakeCloud):
    with TestClient(cloud_app(fake)) as c:
        # The Cloud takes display names without control characters; the node lets this one through to it.
        r = c.post("/v1/agents", json={"slug": "notes", "call_type": "one-shot", "display_name": "a​b "},
                   headers=ios(fake))
        assert (r.status_code, r.json()) == (422, {
            "error": "invalid", "message": "Wristcall Cloud refused this agent; check its fields"})


def test_custom_plan_refuses_operator_voice(fake: FakeCloud):
    fake.plans["acc-custom"] = CUSTOM_PLAN
    fake.plans["acc-full"] = FULL_PLAN
    with TestClient(cloud_app(fake)) as c:
        custom = ios(fake, "acc-custom")
        r = c.post("/v1/agents", json={"slug": "notes", "call_type": "one-shot", "stt": {"provider": "stt"}},
                   headers=custom)
        assert (r.status_code, r.json()) == (422, {
            "error": "needs_voice_url", "message": "stt: this plan needs your own endpoint."})
        r = c.post("/v1/agents", json={"slug": "talk", "stt": OWN_STT, "action": LLM}, headers=custom)
        assert (r.status_code, r.json()) == (422, {
            "error": "needs_voice_url", "message": "tts: this plan needs your own endpoint."})
        r = c.post("/v1/agents", json={"slug": "notes", "call_type": "one-shot", "stt": OWN_STT}, headers=custom)
        assert r.status_code == 201, r.text
        r = c.patch("/v1/agents/notes", json={"stt": {"provider": "stt"}}, headers=custom)
        assert (r.status_code, r.json()["error"]) == (422, "needs_voice_url")
        assert not any(a["account"] == "acc-custom" and a["spec"]["stt"] == {"provider": "stt"}
                       for a in fake.agents.values())

        r = c.post("/v1/agents", json={"slug": "notes", "call_type": "one-shot"}, headers=ios(fake, "acc-full"))
        assert r.status_code == 201, r.text

        r = c.post("/v1/agents", json={"slug": "notes", "call_type": "one-shot"}, headers=ios(fake, "acc-none"))
        assert (r.status_code, r.json()) == (402, NO_PLAN)


# --- /v1/me and push ---------------------------------------------------------------------------------------------


def test_me_cloud_mode(fake: FakeCloud):
    fake.used_s[ACCOUNT] = 120
    with TestClient(cloud_app(fake)) as c:
        assert c.post("/v1/agents", json=NOTES, headers=ios(fake)).status_code == 201
        for headers, device, platform in ((watch(fake), "dev-watch", "watchos"), (ios(fake), "dev-ios", "ios")):
            r = c.get("/v1/me", headers=headers)
            assert r.status_code == 200, r.text
            me = r.json()
            assert me["device_id"] == device and me["device_name"] == platform
            assert me["user"] == {"id": ACCOUNT, "handle": ACCOUNT, "display_name": ""}
            assert [a["slug"] for a in me["agents"]] == ["notes"]
            assert "stt" not in me["agents"][0]
            assert me["profiles"] == [{"name": "notes", "display_name": "notes"}]
            assert me["plan"] == FULL_PLAN
            assert me["usage"]["used_s"] == 120 and me["usage"]["remaining_s"] == fake.quota_s - 120

        r = c.get("/v1/me", headers=ios(fake, "acc-none"))
        assert r.status_code == 200
        assert r.json()["plan"] is None and r.json()["agents"] == []
        assert c.get("/v1/me").status_code == 401


def test_push_put_delete_cloud_mode(fake: FakeCloud):
    app = cloud_app(fake)  # no `push` block: in cloud mode the Cloud is the relay
    with TestClient(app) as c:
        assert c.put("/v1/push", json={"push_key": PUSH_KEY}, headers=ios(fake)).status_code == 204
        assert c.put("/v1/push", json={"push_key": PUSH_KEY}, headers=watch(fake)).status_code == 204
        assert fake.push_targets == {f"{ACCOUNT}:dev-ios": PUSH_KEY, f"{ACCOUNT}:dev-watch": PUSH_KEY}
        assert c.delete("/v1/push", headers=ios(fake)).status_code == 204
        assert fake.push_targets == {f"{ACCOUNT}:dev-watch": PUSH_KEY}

        r = c.put("/v1/push", json={"push_key": "nope"}, headers=ios(fake))
        assert (r.status_code, r.json()["error"]) == (422, "invalid")
        assert c.put("/v1/push", json={"push_key": PUSH_KEY}).status_code == 401

        async def refused(*_args, **_kwargs):
            raise ValueError("the Cloud answered 422")

        app.state.storage.push.set = refused
        r = c.put("/v1/push", json={"push_key": PUSH_KEY}, headers=ios(fake))
        assert (r.status_code, r.json()["error"]) == (422, "invalid")
        assert PUSH_KEY not in r.text


# --- history -----------------------------------------------------------------------------------------------------


def seed(fake: FakeCloud, texts: list[tuple[str, str]], **fields) -> str:
    """A call in the FakeCloud whose entries are sealed as a node seals them (account-scoped AAD and index key)."""
    codec = HistoryCodec(cloud_config().history.key()).for_account(ACCOUNT)
    call_id = fake.seed_call(ACCOUNT, **fields)
    for seq, (role, text) in enumerate(texts):
        stored, sealed = codec.seal(text, aad_scoped(ACCOUNT, call_id, seq))
        fake.entries.setdefault(call_id, {})[seq] = {
            "seq": seq, "role": role, "text": stored, "sealed": sealed, "error": None, "at": 1.0,
            "term_hashes": codec.index_terms(text),
        }
    return call_id


def test_history_list_get_search_export_delete(fake: FakeCloud):
    by_watch = seed(fake, [("user", "buy oat milk tomorrow")], device_id="dev-watch", created_at=100.0)
    by_ios = seed(fake, [("user", "call the dentist")], device_id="dev-ios", created_at=200.0)
    still_open = seed(fake, [], device_id="dev-watch", created_at=300.0, open=True)
    with TestClient(cloud_app(fake)) as c:
        page = c.get("/v1/calls", headers=ios(fake)).json()
        assert [v["id"] for v in page["calls"]] == [still_open, by_ios, by_watch]
        assert page["calls"][2]["text"] == "buy oat milk tomorrow"

        found = c.get("/v1/calls", params={"q": "dentist"}, headers=ios(fake)).json()["calls"]
        assert [v["id"] for v in found] == [by_ios]
        assert c.get("/v1/calls", params={"q": "nothing-like-this"}, headers=ios(fake)).json()["calls"] == []

        # B3: the iPhone reads the call the watch made; the watch reads only its own.
        r = c.get(f"/v1/calls/{by_watch}", headers=ios(fake))
        assert r.status_code == 200 and r.json()["text"] == "buy oat milk tomorrow"
        assert c.get(f"/v1/calls/{by_watch}", headers=watch(fake)).status_code == 200
        assert c.get(f"/v1/calls/{by_ios}", headers=watch(fake)).status_code == 404
        assert c.get("/v1/calls", headers=watch(fake)).status_code == 403

        r = c.get("/v1/calls/export", params={"format": "json"}, headers=ios(fake))
        assert r.status_code == 200
        exported = r.json()["calls"]
        # The Cloud exports oldest first.
        assert [v["id"] for v in exported] == [by_watch, by_ios, still_open]
        assert exported[0]["text"] == "buy oat milk tomorrow"
        md = c.get("/v1/calls/export", params={"format": "md"}, headers=ios(fake))
        assert md.status_code == 200 and "call the dentist" in md.text

        r = c.delete(f"/v1/calls/{still_open}", headers=ios(fake))
        assert (r.status_code, r.json()) == (409, {"error": "call_open", "message": "This call is still open."})
        assert c.delete(f"/v1/calls/{by_ios}", headers=ios(fake)).status_code == 204
        assert c.get(f"/v1/calls/{by_ios}", headers=ios(fake)).status_code == 404
        assert c.delete("/v1/calls", params={"all": "true"}, headers=ios(fake)).json() == {"deleted": 1}
        assert set(fake.calls) == {still_open}


def test_export_with_no_calls_is_an_empty_document(fake: FakeCloud):
    with TestClient(cloud_app(fake)) as c:
        r = c.get("/v1/calls/export", params={"format": "json"}, headers=ios(fake))
    assert r.status_code == 200 and r.json()["calls"] == []


def test_export_with_the_cloud_down_is_a_real_503(fake: FakeCloud):
    seed(fake, [("user", "hello")], created_at=100.0)
    with TestClient(cloud_app(fake)) as c:
        wait_ready(c)
        assert c.get("/v1/calls", headers=ios(fake)).status_code == 200
        fake.down = True
        r = c.get("/v1/calls/export", params={"format": "json"}, headers=ios(fake))
    assert (r.status_code, r.json()) == (503, CLOUD_DOWN)


# --- the node itself ---------------------------------------------------------------------------------------------


def node_for(fake: FakeCloud, **kwargs) -> CloudNode:
    config = cloud_config()
    return CloudNode.build(config, slots=CallSlots(config.limits.max_concurrent_calls), http=fake.http(), **kwargs)


def proof(fake: FakeCloud, account: str = ACCOUNT) -> UserProof:
    return UserProof(account, "dev-ios", "ios", fake.user_token(account, "dev-ios", "ios"))


async def test_node_plan_is_cached_per_account(fake: FakeCloud):
    clock = [0.0]
    node = node_for(fake, clock=lambda: clock[0])
    assert await node.plan(proof(fake)) == "full"
    fake.plans[ACCOUNT] = None
    assert await node.plan(proof(fake)) == "full"  # kept 30 s
    assert await node.plan(proof(fake, "acc-other")) is None
    fake.plans["acc-other"] = FULL_PLAN
    assert await node.plan(proof(fake, "acc-other")) == "full"  # "no plan" is not kept: a plan just bought counts
    clock[0] = 31.0
    assert await node.plan(proof(fake)) is None
    me_calls = [r for r in fake.requests if r["path"] == "/v1/node/me"]
    assert len(me_calls) == 4


async def test_node_me_opens_the_agents(fake: FakeCloud):
    node = node_for(fake)
    token = current_proof.set(proof(fake))
    try:
        await node.storage.agents.create(AgentRecord(
            id="ag_0123456789ab", user_id=ACCOUNT, slug="talk", display_name="Talk", icon="waveform",
            call_type="conversation", position=0,
            spec={"language": "en", "stt": {"provider": "stt"}, "action": LLM, "tts": {"provider": "tts"}},
            created_at=1.0, updated_at=1.0,
        ))
    finally:
        current_proof.reset(token)
    me = await node.me(proof(fake))
    assert me["plan"] == FULL_PLAN and me["usage"]["remaining_s"] == fake.quota_s
    assert [a.slug for a in me["agents"]] == ["talk"]
    assert me["agents"][0].spec["action"]["api_key"] == "sk-agent-secret"  # opened with the proof's account


@pytest.mark.parametrize("body", [
    {"plan": 3, "usage": {}, "agents": []},
    {"plan": {"source": "beta"}, "usage": {}, "agents": []},
    {"plan": None, "usage": {}, "agents": "no"},
    {"plan": None, "agents": []},
    ["not", "an", "object"],
])
async def test_node_me_odd_answer_is_cloud_unavailable(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    config = cloud_config()
    node = CloudNode.build(config, slots=CallSlots(config.limits.max_concurrent_calls),
                           http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(CloudUnavailable):
        await node.me(UserProof(ACCOUNT, "dev-ios", "ios", "token"))


async def test_node_me_maps_account_errors(fake: FakeCloud):
    node = node_for(fake)
    fake.revoked.add("dev-ios")
    with pytest.raises(DeviceRevoked):
        await node.me(proof(fake))


async def test_node_start_beats_and_stop_drains_outbox(fake: FakeCloud):
    node = node_for(fake, outbox_max_delay_s=0.05)
    call_id = fake.seed_call(ACCOUNT, open=True)
    grant = fake.issue_grant(call_id)
    await node.start()
    await asyncio.wait_for(node.ready.wait(), 2.0)
    assert node.accepting
    node.outbox.put(OutboxItem(call_id, "patch", f"/v1/node/calls/{call_id}", {"attempts": 2}, grant))
    assert await node.stop(drain_s=2.0) == 0
    assert fake.calls[call_id]["attempts"] == 2
    beats = len(fake.heartbeats)
    await asyncio.sleep(0.15)
    assert len(fake.heartbeats) == beats  # the heartbeat stopped


async def test_node_stop_with_the_cloud_down_gives_up_in_time(fake: FakeCloud):
    node = node_for(fake, outbox_max_delay_s=0.05)
    await node.start()
    await asyncio.wait_for(node.ready.wait(), 2.0)
    fake.down = True
    node.outbox.put(OutboxItem("c_0", "patch", "/v1/node/calls/c_0", {"attempts": 1}, "g"))
    started = time.monotonic()
    assert await node.stop(drain_s=0.2) == 1
    assert time.monotonic() - started < 1.0


def test_node_build_needs_cloud_mode():
    config = fake_config()
    with pytest.raises(ValueError):
        CloudNode.build(config, slots=CallSlots(config.limits.max_concurrent_calls), http=httpx.AsyncClient())


def test_node_repr_holds_no_secret(fake: FakeCloud):
    node = node_for(fake)
    assert NODE_SECRET not in repr(node) and NODE_SECRET not in repr(node.client)


async def test_closing_an_export_closes_the_cloud_stream(fake: FakeCloud):
    # The export route closes its views when the client goes away; that reaches the HTTP stream of the Cloud's export.
    from wristcall.history import History

    for at in (100.0, 200.0, 300.0):
        seed(fake, [("user", "hello")], created_at=at)
    node = node_for(fake)
    client = node.storage.calls._client
    streamed = client.stream_lines
    closed = []

    async def stream_lines(*args, **kwargs):
        try:
            async for line in streamed(*args, **kwargs):
                yield line
        finally:
            closed.append(True)

    client.stream_lines = stream_lines
    history = History(node.storage, HistoryCodec(cloud_config().history.key()), cloud_config().history,
                      account_scoped=True)
    token = current_proof.set(proof(fake))
    try:
        views = history.details(ACCOUNT)
        assert (await anext(views))["text"] == "hello"
        await views.aclose()
    finally:
        current_proof.reset(token)
    assert closed == [True]
