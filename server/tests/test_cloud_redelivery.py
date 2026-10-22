"""Cloud mode: redelivery through the Cloud (B1), the node draining its calls, and the uvicorn server that drains on
the first SIGTERM (I8)."""

import asyncio
import json
import signal
import time

import httpx
import pytest
import respx
import uvicorn
from cloud_app_fixtures import PUBLIC_HOSTS, bearer, cloud_app, cloud_config, new_fake, wait_ready
from conftest import fake_resolver
from fake_cloud import FULL_PLAN, FakeCloud
from fastapi.testclient import TestClient

from wristcall.admission import CallSlots
from wristcall.cloud_node.proof import UserProof, current_proof
from wristcall.cloud_node.redelivery import CloudRedelivery
from wristcall.cloud_node.draining import DrainingServer
from wristcall.cloud_node.node import CloudNode
from wristcall.delivery import DeliveryPolicy
from wristcall.history import History, aad_scoped
from wristcall.history_codec import HistoryCodec
from wristcall.oneway import Background
from wristcall.storage import CallRecord, CloudUnavailable

ACCOUNT = "acc-1"
HOOK_URL = "https://hooks.example.test/in"
HOOK = {"type": "webhook", "url": HOOK_URL}


@pytest.fixture
def fake() -> FakeCloud:
    cloud = new_fake()
    cloud.plans[ACCOUNT] = FULL_PLAN
    return cloud


def ios(fake: FakeCloud) -> dict[str, str]:
    return bearer(fake, ACCOUNT, "dev-ios", "ios")


def seed(fake: FakeCloud, texts: list[tuple[str, str]], **fields) -> str:
    """A finished one-way call in the FakeCloud, its entries sealed as a node seals them."""
    codec = HistoryCodec(cloud_config().history.key()).for_account(ACCOUNT)
    fields = {"call_type": "one-shot", "status": "failed", "error": "delivery_failed", "attempts": 3,
              "last_http_status": 500, "delivery": "webhook", "created_at": 100.0, **fields}
    call_id = fake.seed_call(ACCOUNT, **fields)
    fake.calls[call_id].update(ended_at=110.0, finished_at=160.0)
    for seq, (role, text) in enumerate(texts):
        stored, sealed = codec.seal(text, aad_scoped(ACCOUNT, call_id, seq))
        fake.entries.setdefault(call_id, {})[seq] = {
            "seq": seq, "role": role, "text": stored, "sealed": sealed, "error": None, "at": 1.0,
            "term_hashes": codec.index_terms(text),
        }
    return call_id


def redeliver_app(fake: FakeCloud, **kwargs):
    hosts = {**PUBLIC_HOSTS, "hooks.example.test": ["93.184.216.35"]}
    return cloud_app(fake, resolver=fake_resolver(hosts), delivery_policy=DeliveryPolicy(1.0, (0.0, 0.0)), **kwargs)


def create_agent(c: TestClient, fake: FakeCloud, **body) -> dict:
    r = c.post("/v1/agents", json={"slug": "hook", "call_type": "one-shot", "action": HOOK, **body}, headers=ios(fake))
    assert r.status_code == 201, r.text
    return r.json()


def wait_for(check, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def paths(fake: FakeCloud, method: str, suffix: str) -> list[str]:
    return [r["path"] for r in fake.requests if r["method"] == method and r["path"].endswith(suffix)]


@pytest.fixture
def hook():
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        yield mock.post(HOOK_URL).mock(return_value=httpx.Response(204))


# --- redelivery ----------------------------------------------------------------------------------------------------

def test_redeliver_cloud_mode(fake: FakeCloud, hook):
    with TestClient(redeliver_app(fake)) as c:
        wait_ready(c)
        agent = create_agent(c, fake)
        call_id = seed(fake, [("user", "buy oat milk")], agent_id=agent["id"])
        r = c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake))
        assert r.status_code == 202, r.text
        view = r.json()
        assert (view["id"], view["status"], view["error"]) == (call_id, "processing", None)
        assert view["text"] == "buy oat milk"
        wait_for(lambda: fake.calls[call_id]["status"] == "delivered")
        stored = fake.calls[call_id]
        assert (stored["error"], stored["attempts"], stored["last_http_status"]) == (None, 4, 204)
        assert stored["finished_at"] is not None
        sent = hook.calls.last.request
        assert sent.headers["Idempotency-Key"] == call_id
        body = json.loads(sent.content)
        assert body["text"] == "buy oat milk" and body["call_id"] == call_id and body["agent"]["id"] == agent["id"]
        assert paths(fake, "POST", "/finish") == []  # a redelivery only PATCHes (5.4.1)
        assert paths(fake, "PATCH", call_id)
        node = c.app.state.cloud_node
        assert node.registry.grant(call_id) is None and node.registry.ids() == []
        assert c.get(f"/v1/calls/{call_id}", headers=ios(fake)).json()["status"] == "delivered"


def test_redeliver_never_lists_the_call_as_open(fake: FakeCloud):
    # The grant is held while the webhook is called, but the call is not one the node holds open (heartbeat).
    seen: list[tuple[list[str], bool]] = []
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()

        def record(_request):
            seen.append((node.registry.ids(), node.registry.grant(call_id) is not None))
            return httpx.Response(204)

        mock.post(HOOK_URL).mock(side_effect=record)
        with TestClient(redeliver_app(fake)) as c:
            wait_ready(c)
            node = c.app.state.cloud_node
            agent = create_agent(c, fake)
            call_id = seed(fake, [("user", "hello")], agent_id=agent["id"])
            assert c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake)).status_code == 202
            wait_for(lambda: fake.calls[call_id]["status"] == "delivered")
            assert node.heartbeat.body()["active_call_ids"] == []
    assert seen == [([], True)]


def test_redeliver_history_delivery_is_no_webhook_without_the_cloud(fake: FakeCloud):
    with TestClient(redeliver_app(fake)) as c:
        wait_ready(c)
        call_id = seed(fake, [("user", "hello")], delivery="history")
        r = c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake))
        assert (r.status_code, r.json()["error"]) == (409, "no_webhook")
        assert paths(fake, "POST", "/redeliver") == []
        assert fake.calls[call_id]["status"] == "failed"


@pytest.mark.parametrize("fields, code", [
    ({"call_type": "conversation", "delivery": None}, "not_one_way"),
    ({"open": True}, "call_open"),
    ({"status": "processing", "error": None}, "busy"),
    ({"status": "delivered", "error": None}, "not_failed"),
    ({"error": "transcription_failed"}, "not_failed"),
])
def test_redeliver_cloud_refusals(fake: FakeCloud, fields, code):
    with TestClient(redeliver_app(fake)) as c:
        wait_ready(c)
        agent = create_agent(c, fake)
        call_id = seed(fake, [("user", "hello")], agent_id=agent["id"], **fields)
        if fields.get("open"):
            fake.calls[call_id]["open"] = True
        before = dict(fake.calls[call_id])
        r = c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake))
        assert (r.status_code, r.json()["error"]) == (409, code)
        assert paths(fake, "POST", "/redeliver") == [f"/v1/node/calls/{call_id}/redeliver"]
        assert fake.calls[call_id] == before
    if code == "call_open":
        assert r.json()["message"] == "This call is still open."


@pytest.mark.parametrize("case, code", [
    ("agent_deleted", "agent_gone"),
    ("no_text", "no_text"),
    ("no_webhook", "no_webhook"),
    ("interrupted", "agent_gone"),
])
def test_redeliver_refused_after_the_claim_puts_the_call_back(fake: FakeCloud, case, code):
    with TestClient(redeliver_app(fake)) as c:
        wait_ready(c)
        node = c.app.state.cloud_node
        agent = create_agent(c, fake) if case != "no_webhook" else create_agent(c, fake, action=None)
        texts = [] if case == "no_text" else [("user", "hello")]
        error = "interrupted" if case == "interrupted" else "delivery_failed"
        agent_id = "ag_0000000000ff" if case in ("agent_deleted", "interrupted") else agent["id"]
        call_id = seed(fake, texts, agent_id=agent_id, error=error)
        r = c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake))
        assert (r.status_code, r.json()["error"]) == (409, code)
        assert paths(fake, "POST", "/redeliver") == [f"/v1/node/calls/{call_id}/redeliver"]
        # Put back as it was, by a queued PATCH (the Cloud had marked it processing).
        wait_for(lambda: fake.calls[call_id]["status"] == "failed")
        stored = fake.calls[call_id]
        assert (stored["error"], stored["attempts"], stored["finished_at"]) == (error, 3, 160.0)
        assert paths(fake, "POST", "/finish") == []
        assert node.registry.grant(call_id) is None and node.registry.ids() == []


def test_redeliver_webhook_refuses_again(fake: FakeCloud):
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        mock.post(HOOK_URL).mock(return_value=httpx.Response(500))
        with TestClient(redeliver_app(fake)) as c:
            wait_ready(c)
            agent = create_agent(c, fake)
            call_id = seed(fake, [("user", "hello")], agent_id=agent["id"])
            assert c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake)).status_code == 202
            wait_for(lambda: fake.calls[call_id]["status"] == "failed")
            stored = fake.calls[call_id]
            # Three new attempts on top of the three before, as self-hosted.
            assert (stored["error"], stored["attempts"], stored["last_http_status"]) == ("delivery_failed", 6, 500)
            assert paths(fake, "POST", "/finish") == []


def test_redeliver_webhook_goes_through_the_public_only_client(fake: FakeCloud):
    # The user's webhook now resolving to a private address is refused (limits.public_only, forced in cloud mode): the
    # node's own resolver is asked (the plain client would ask the system's).
    hosts = {**PUBLIC_HOSTS, "hooks.example.test": ["93.184.216.35"]}
    table = fake_resolver(hosts)
    asked: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        asked.append(host)
        return await table(host, port)

    app = cloud_app(fake, resolver=resolve, delivery_policy=DeliveryPolicy(1.0, (0.0, 0.0)))
    with TestClient(app) as c:
        wait_ready(c)
        agent = create_agent(c, fake)
        hosts["hooks.example.test"] = ["10.0.0.7"]  # now it points inside
        asked.clear()
        call_id = seed(fake, [("user", "hello")], agent_id=agent["id"])
        assert c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake)).status_code == 202
        wait_for(lambda: fake.calls[call_id]["status"] == "failed")
        stored = fake.calls[call_id]
        assert (stored["error"], stored["last_http_status"]) == ("delivery_failed", None)
        assert "hooks.example.test" in asked


def test_redeliver_route_is_open_in_cloud_mode(fake: FakeCloud):
    # The route left the not_in_cloud_mode list: an unknown call is a plain 404 not_found.
    with TestClient(redeliver_app(fake)) as c:
        wait_ready(c)
        r = c.post("/v1/calls/c_0123456789abcdef/redeliver", headers=ios(fake))
        assert r.status_code == 404 and r.json()["error"] == "not_found"


@pytest.mark.parametrize("answer", [
    {"grant": "g-1", "call": "not an object", "entries": [], "agent": None},
    {"grant": "g-1", "entries": [], "agent": None},
])
async def test_redeliver_odd_answer_is_cloud_unavailable_and_puts_the_call_back(answer):
    # The Cloud handed the call over (grant) but the rest of its answer is unusable: 503, and the call goes back.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answer)

    config = cloud_config()
    node = CloudNode.build(config, slots=CallSlots(config.limits.max_concurrent_calls),
                           http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    node.ready.set()  # a node taking work (a starting or draining one refuses redelivery: busy)
    history = History(node.storage, HistoryCodec(config.history.key()), config.history, account_scoped=True)
    redelivery = CloudRedelivery(node, history, config, Background(grace_s=0.1), httpx.AsyncClient())
    record = CallRecord(
        id="c_0123456789abcdef", user_id=ACCOUNT, agent_id="ag_0123456789ab", device_id="dev-watch",
        call_type="one-shot", status="failed", error="interrupted", attempts=2, last_http_status=None,
        created_at=100.0, updated_at=160.0, ended_at=110.0, finished_at=160.0, delivery="webhook",
    )
    token = current_proof.set(UserProof(ACCOUNT, "dev-ios", "ios", "token"))
    try:
        with pytest.raises(CloudUnavailable):
            await redelivery.start(record)
    finally:
        current_proof.reset(token)
    [item] = node.outbox.pending()
    assert (item.kind, item.path, item.grant) == ("patch", f"/v1/node/calls/{record.id}", "g-1")
    assert item.body == {"status": "failed", "error": "interrupted", "finished_at": 160.0}
    assert node.registry.grant(record.id) is None


# --- draining ------------------------------------------------------------------------------------------------------

def node_for(fake: FakeCloud) -> tuple[CloudNode, CallSlots]:
    config = cloud_config()
    slots = CallSlots(config.limits.max_concurrent_calls)
    return CloudNode.build(config, slots=slots, http=fake.http()), slots


async def test_drain_calls_waits_for_open_calls(fake: FakeCloud):
    node, slots = node_for(fake)
    node.ready.set()
    slot = slots.try_acquire("conversation")
    assert node.accepting
    asyncio.get_running_loop().call_later(0.1, slot.release)
    started = time.monotonic()
    left = await node.drain_calls(slots, 2.0, poll_s=0.01)
    assert left == 0 and 0.09 <= time.monotonic() - started < 1.0
    assert node.draining and not node.accepting


async def test_drain_calls_gives_up_after_its_time(fake: FakeCloud):
    node, slots = node_for(fake)
    slots.try_acquire("one_way")
    slots.try_acquire("conversation")
    started = time.monotonic()
    assert await node.drain_calls(slots, 0.1, poll_s=0.01) == 2
    assert time.monotonic() - started < 1.0


async def test_draining_node_tells_the_cloud_it_takes_no_calls(fake: FakeCloud):
    node, slots = node_for(fake)
    slots.try_acquire("conversation")
    assert node.heartbeat.body()["capacity"] == {"conversation": 2, "one_way": 2}
    assert await node.drain_calls(slots, 0.1, poll_s=0.01) == 1
    beat = fake.heartbeats[-1]  # sent at once, not at the next beat
    assert beat["capacity"] == {"conversation": 0, "one_way": 0}
    assert beat["active_by_type"] == {"conversation": 1, "one_way": 0}


async def test_drain_with_no_calls_returns_at_once(fake: FakeCloud):
    node, slots = node_for(fake)
    fake.down = True  # the beat telling the Cloud is best effort
    started = time.monotonic()
    assert await node.drain_calls(slots, 5.0) == 0
    assert time.monotonic() - started < 1.0


def test_health_is_draining_while_the_node_drains(fake: FakeCloud):
    with TestClient(cloud_app(fake)) as c:
        wait_ready(c)
        node = c.app.state.cloud_node
        slot = c.app.state.call_slots.try_acquire("conversation")
        c.portal.start_task_soon(lambda: node.drain_calls(c.app.state.call_slots, 2.0, poll_s=0.01))
        wait_for(lambda: node.draining)
        r = c.get("/v1/health")
        assert (r.status_code, r.json()["status"]) == (503, "draining")
        slot.release()


# --- the uvicorn server that drains first --------------------------------------------------------------------------

def draining_server(node: CloudNode, slots: CallSlots, drain_s: float = 2.0) -> DrainingServer:
    async def app(scope, receive, send):  # never called: the server is not started
        raise AssertionError

    return DrainingServer(uvicorn.Config(app, timeout_graceful_shutdown=5), node=node, slots=slots, drain_s=drain_s,
                          poll_s=0.01)


async def tick_until_exit(server: DrainingServer, timeout_s: float = 2.0) -> float:
    started = time.monotonic()
    while not await server.on_tick(1):
        assert time.monotonic() - started < timeout_s, "the server never exits"
        await asyncio.sleep(0.01)
    return time.monotonic() - started


async def test_first_signal_drains_the_calls_then_exits(fake: FakeCloud):
    node, slots = node_for(fake)
    slot = slots.try_acquire("conversation")
    server = draining_server(node, slots)
    server.handle_exit(signal.SIGTERM, None)  # what the signal handler runs: only flags, no loop work
    assert not server.should_exit and not node.draining
    assert await server.on_tick(1) is False  # the loop's tick starts the drain
    await asyncio.sleep(0.01)
    assert node.draining and not node.accepting
    for _ in range(5):
        assert await server.on_tick(1) is False  # the call is still open: uvicorn keeps serving it
        await asyncio.sleep(0.01)
    slot.release()
    await tick_until_exit(server)
    assert server.should_exit and not server.force_exit


async def test_server_exits_when_the_drain_runs_out_of_time(fake: FakeCloud):
    node, slots = node_for(fake)
    slots.try_acquire("conversation")
    server = draining_server(node, slots, drain_s=0.1)
    server.handle_exit(signal.SIGTERM, None)
    assert await tick_until_exit(server) < 1.0
    assert not server.force_exit


async def test_second_signal_forces_the_exit(fake: FakeCloud):
    node, slots = node_for(fake)
    slots.try_acquire("conversation")
    server = draining_server(node, slots, drain_s=30.0)
    server.handle_exit(signal.SIGTERM, None)
    assert await server.on_tick(1) is False
    server.handle_exit(signal.SIGINT, None)
    assert server.force_exit and server.should_exit
    assert await server.on_tick(1) is True
    server.servers = []  # what startup would have set
    await server.shutdown()  # cancels the drain still waiting
    assert server.drain_task is not None and server.drain_task.cancelled()


async def test_no_signal_no_drain(fake: FakeCloud):
    node, slots = node_for(fake)
    server = draining_server(node, slots)
    assert await server.on_tick(1) is False
    assert not node.draining and server.drain_task is None
