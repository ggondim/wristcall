"""Calls of the Cloud nodes: opening (plan, quota, one open call), the grant, entries, status, `finish` and the
month's usage (`/v1/node/calls*`, `close_call`)."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from node_fixtures import (
    NODE_A,
    NODE_B,
    add_device,
    db,
    grant_plan,
    new_account,
    node_app,
    node_client,
    node_config,
    node_headers,
    nodes,
    signing_key,
    user_token,
)
from wristcall_cloud.cloud_store import usage_month
from wristcall_cloud.node_auth import GRANT_SLACK_S, NodeCaller, issue_grant, verify_grant
from wristcall_cloud.node_core import close_call

HOME = {"_id": "home", "url": NODE_A}
HASH_A = "x" + "0123456789abcdef"
HASH_B = "x" + "fedcba9876543210"


def agent_body(n: int, call_type: str, **spec) -> dict:
    return {
        "id": f"ag_{n:012x}", "slug": f"agent-{n}", "display_name": f"Agent {n}", "icon": "waveform", "color": "blue",
        "call_type": call_type, "spec": {"stt": {"sealed": "AAAA"}, **spec},
    }


class World:
    """One account with an iPhone and a watch, an agent of each kind, and the headers a node sends for them."""

    def __init__(self, client, mongo_db, nodes, signing_key, node_app, first_agent: int = 1) -> None:
        self.client, self.mongo_db, self.nodes = client, mongo_db, nodes
        self.signing_key, self.app = signing_key, node_app
        self.account, self.ios_device, self.key = new_account(client, "ios")
        self.watch_device = add_device(client, self.key, "watchos")
        grant_plan(mongo_db, self.account)
        ios = self.user(self.ios_device, "ios")
        self.oneshot = self._agent(ios, first_agent, "one-shot", retention_days=30)["id"]
        self.convo = self._agent(ios, first_agent + 1, "conversation")["id"]

    def user(self, device: str, platform: str, node: str = "home", url: str = NODE_A) -> dict:
        token = user_token(self.signing_key, self.account, device, platform, url)
        return node_headers(self.nodes[node], user=token)

    @property
    def watch(self) -> dict:
        return self.user(self.watch_device, "watchos")

    @property
    def watch_away(self) -> dict:
        return self.user(self.watch_device, "watchos", "away", NODE_B)

    def _agent(self, headers: dict, n: int, call_type: str, **spec) -> dict:
        r = self.client.post("/v1/node/agents", headers=headers, json=agent_body(n, call_type, **spec))
        assert r.status_code == 201, r.text
        return r.json()

    def open(self, agent: str | None = None, call_type: str = "one-shot", headers: dict | None = None, **extra):
        body = {"agent": agent or self.oneshot, "call_type": call_type, **extra}
        return self.client.post("/v1/node/calls", headers=headers or self.watch, json=body)

    def opened(self, *args, **kwargs) -> dict:
        r = self.open(*args, **kwargs)
        assert r.status_code == 201, r.text
        return r.json()

    def node_with(self, opened: dict, node: str = "home") -> dict:
        return node_headers(self.nodes[node], grant=opened["grant"])

    def grant_for(self, call_id: str, *, now: float, max_s: int = 1800, node_id: str = "home") -> dict:
        caller = NodeCaller(
            account=self.account, device=self.watch_device, platform="watchos", node={"_id": node_id, "url": NODE_A}
        )
        token = issue_grant(
            self.signing_key, issuer="https://cloud.test", caller=caller, call_id=call_id, max_s=max_s, now=now
        )
        return node_headers(self.nodes[node_id], grant=token)

    def call_doc(self, call_id: str) -> dict:
        with db(self.mongo_db) as d:
            return d["cloud_calls"].find_one({"_id": call_id})

    def usage(self, month: str | None = None) -> dict | None:
        with db(self.mongo_db) as d:
            return d["usage"].find_one({"_id": f"{self.account}:{month or usage_month(time.time())}"})

    def set_usage(self, total_s: int) -> None:
        month = usage_month(time.time())
        with db(self.mongo_db) as d:
            d["usage"].replace_one(
                {"_id": f"{self.account}:{month}"},
                {"_id": f"{self.account}:{month}", "account": self.account, "month": month, "total_s": total_s},
                upsert=True,
            )

    def finish(self, opened: dict, **body):
        return self.client.post(
            f"/v1/node/calls/{opened['call_id']}/finish",
            headers={**self.node_with(opened), "Content-Type": "application/json"},
            content=raw(body),
        )


@pytest.fixture
def world(node_client, mongo_db, nodes, signing_key, node_app) -> World:
    return World(node_client, mongo_db, nodes, signing_key, node_app)


def raw(body) -> bytes:
    """JSON as a client could write it, `NaN` and `Infinity` included (the test client refuses to)."""
    return json.dumps(body).encode()


def entry(seq: int, **over) -> dict:
    return {
        "role": "user", "text": f"sealed-{seq}", "sealed": True, "error": None, "at": 1000.5 + seq,
        "term_hashes": [HASH_A, HASH_B], **{"seq": seq, **over},
    }


def test_open_call_returns_grant_and_limits(world):
    before = time.time()
    one = world.open()
    assert one.status_code == 201, one.text
    body = one.json()
    assert re.fullmatch(r"c_[0-9a-f]{16}", body["call_id"])
    assert body["quota_s"] == 36000 and body["max_s"] == 1800
    assert body["plan"]["plan"] == "full" and body["agent"]["id"] == world.oneshot
    assert before <= body["started_at"] <= time.time()
    got = verify_grant(SimpleNamespace(app=world.app), HOME, body["call_id"], body["grant"], allow_expired=False)
    assert (got.account, got.device, got.platform, got.node_id) == (
        world.account, world.watch_device, "watchos", "home"
    )
    doc = world.call_doc(body["call_id"])
    assert doc["open"] is True and doc["status"] == "recording" and doc["node"] == "home"
    assert doc["agent"] == world.oneshot and doc["agent_slug"] == "agent-1" and doc["agent_name"] == "Agent 1"
    assert doc["device"] == world.watch_device and doc["platform"] == "watchos" and doc["max_s"] == 1800
    assert doc["created_at"] == body["started_at"] and doc["seconds"] is None and doc["attempts"] == 0
    # The agent says 30 days: the call and its history expire that long after it began.
    assert doc["expires_at"] == pytest.approx(doc["created_at"] + 30 * 86400)
    assert doc["purge_at"].timestamp() == pytest.approx(doc["expires_at"], abs=0.01)

    world.finish(body, status="delivered", seconds=1)
    convo = world.open(world.convo, "conversation")
    assert convo.status_code == 201 and convo.json()["max_s"] == 3600
    default = world.call_doc(convo.json()["call_id"])
    assert default["expires_at"] == pytest.approx(default["created_at"] + 90 * 86400)


def test_open_call_refusals(world):
    with db(world.mongo_db) as d:
        d["entitlements"].delete_one({"_id": world.account})
    r = world.open()
    assert r.status_code == 402 and r.json()["error"] == "plan_required"
    grant_plan(world.mongo_db, world.account)

    stranger = World(world.client, world.mongo_db, world.nodes, world.signing_key, world.app, first_agent=10)
    r = world.open(stranger.oneshot)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = world.open("nope")
    assert r.status_code == 404
    r = world.open(call_type="conversation")
    assert r.status_code == 422 and r.json()["error"] == "invalid"
    for bad in ({"agent": 1}, {"agent": None}, {"call_type": "radio"}, {"call_type": None}, {"request_id": "short"},
                {"request_id": "bad id!bad id!"}, {"request_id": 12345678}):
        body = {"agent": world.oneshot, "call_type": "one-shot", **bad}
        r = world.client.post("/v1/node/calls", headers=world.watch, json=body)
        assert r.status_code == 422, bad
    by_slug = world.open("agent-1")
    assert by_slug.status_code == 201
    again = world.open()
    assert again.status_code == 409 and again.json()["error"] == "too_many_calls"
    # Another account is not held back by this one's open call.
    assert stranger.open().status_code == 201


def test_open_call_retry_with_request_id_returns_the_same_call(world):
    first = world.opened(request_id="req-0001-abcd")
    again = world.open(request_id="req-0001-abcd")
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["call_id"] == first["call_id"] and body["max_s"] == first["max_s"]
    assert body["started_at"] == first["started_at"] and body["agent"]["id"] == world.oneshot
    verify_grant(SimpleNamespace(app=world.app), HOME, body["call_id"], body["grant"], allow_expired=False)
    other = world.open(request_id="req-0002-abcd")
    assert other.status_code == 409 and other.json()["error"] == "too_many_calls"
    # Once the call is closed the same id opens a new one.
    assert world.finish(first, status="delivered", seconds=5).status_code == 204
    fresh = world.open(request_id="req-0001-abcd")
    assert fresh.status_code == 201 and fresh.json()["call_id"] != first["call_id"]


def test_entry_text_must_be_sealed(world):
    opened = world.opened()
    url = f"/v1/node/calls/{opened['call_id']}/entries"
    headers = world.node_with(opened)
    r = world.client.post(url, headers=headers, json={"entries": [entry(0, sealed=False)]})
    assert r.status_code == 422 and r.json()["error"] == "invalid"
    unmarked = {k: v for k, v in entry(0, text="plain text").items() if k != "sealed"}
    r = world.client.post(url, headers=headers, json={"entries": [unmarked]})
    assert r.status_code == 422
    r = world.client.post(url, headers=headers, json={"entries": [entry(1, text=None, error="stt_failed")]})
    assert r.status_code == 204 and r.content == b""
    with db(world.mongo_db) as d:
        assert d["cloud_call_entries"].count_documents({"call": opened["call_id"]}) == 1


def test_quota_exceeded_and_max_s_capped_by_quota(world):
    world.set_usage(35900)
    opened = world.opened()
    assert opened["max_s"] == 100 and opened["quota_s"] == 100
    assert world.finish(opened, status="delivered", seconds=100).status_code == 204
    r = world.open()
    assert r.status_code == 402 and r.json()["error"] == "quota_exceeded"
    world.set_usage(36000)
    assert world.open().status_code == 402
    world.set_usage(99999)
    assert world.open().json()["error"] == "quota_exceeded"


def test_entries_are_idempotent_by_seq(world):
    opened = world.opened()
    call_id = opened["call_id"]
    url = f"/v1/node/calls/{call_id}/entries"
    headers = world.node_with(opened)
    batch = {"entries": [entry(0), entry(1, role="agent", text=None, error="stt_failed", term_hashes=[])]}
    assert world.client.post(url, headers=headers, json=batch).status_code == 204
    # Resent (the answer was lost), with a different text for seq 0: the first one stays.
    batch["entries"][0] = entry(0, text="changed")
    assert world.client.post(url, headers=headers, json=batch).status_code == 204
    with db(world.mongo_db) as d:
        docs = list(d["cloud_call_entries"].find({"call": call_id}).sort("seq", 1))
    assert [x["_id"] for x in docs] == [f"{call_id}:0", f"{call_id}:1"]
    first, second = docs
    call = world.call_doc(call_id)
    assert first["text"] == "sealed-0" and first["sealed"] is True and first["role"] == "user" and first["at"] == 1000.5
    assert first["term_hashes"] == [HASH_A, HASH_B] and first["error"] is None
    assert second["text"] is None and second["error"] == "stt_failed" and second["role"] == "agent"
    for doc in docs:
        assert doc["account"] == world.account and doc["agent"] == world.oneshot
        assert doc["call_created_at"] == call["created_at"] and doc["expires_at"] == call["expires_at"]
        assert doc["purge_at"] == call["purge_at"]

    def post(entries):
        return world.client.post(url, headers=headers, json={"entries": entries})

    assert post([entry(2, term_hashes=["xzzzzzzzzzzzzzzz"])]).status_code == 422
    assert post([entry(2, term_hashes=["x123"])]).status_code == 422
    assert post([entry(2, term_hashes=[HASH_A.upper()])]).status_code == 422
    assert post([entry(2, term_hashes="x0123456789abcdef")]).status_code == 422
    assert post([entry(n) for n in range(101)]).status_code == 422
    assert post([entry(n) for n in range(100)]).status_code == 204
    # Nothing of a refused batch was stored.
    with db(world.mongo_db) as d:
        assert d["cloud_call_entries"].count_documents({"call": call_id}) == 100


@pytest.mark.parametrize(
    "over",
    [
        {"seq": -1}, {"seq": 100001}, {"seq": True}, {"seq": "1"}, {"seq": 1.5}, {"role": "system"}, {"role": None},
        {"text": 5}, {"text": "x" * (256 * 1024 + 1)}, {"text": "\ud800"}, {"error": "Stt Failed"},
        {"error": "x" * 33}, {"error": 7}, {"at": "now"}, {"at": None}, {"at": float("inf")}, {"sealed": "yes"},
    ],
)
def test_entry_fields_are_validated(world, over):
    opened = world.opened()
    r = world.client.post(
        f"/v1/node/calls/{opened['call_id']}/entries", headers=world.node_with(opened),
        content=raw({"entries": [{**entry(0), **over}]}),
    )
    assert r.status_code == 422, over


def test_entry_body_shapes_are_422(world):
    opened = world.opened()
    url = f"/v1/node/calls/{opened['call_id']}/entries"
    headers = world.node_with(opened)
    for body in ({}, {"entries": "x"}, {"entries": [1]}, {"entries": [entry(0), entry(0)]}):
        assert world.client.post(url, headers=headers, json=body).status_code == 422, body
    assert world.client.post(url, headers=headers, json=[entry(0)]).status_code == 422


def test_grant_of_another_call_or_node_is_401(world):
    # The grant of a call is useless on another call of the same account, and the home node's is useless for the
    # away node (whose credential is what is presented).
    first = world.opened()
    assert world.finish(first, status="delivered", seconds=1).status_code == 204
    second = world.opened()
    other_call = f"/v1/node/calls/{first['call_id']}"
    for method, path, body in (
        ("post", f"{other_call}/entries", {"entries": [entry(0)]}),
        ("patch", other_call, {"status": "processing"}),
        ("post", f"{other_call}/finish", {"status": "delivered", "seconds": 1}),
    ):
        r = world.client.request(method, path, headers=world.node_with(second), json=body)
        assert r.status_code == 401, path
    r = world.client.post(
        f"/v1/node/calls/{second['call_id']}/finish", headers=world.node_with(second, "away"),
        json={"status": "delivered", "seconds": 1},
    )
    assert r.status_code == 401
    # No grant, or the user token instead of the grant.
    r = world.client.post(
        f"/v1/node/calls/{second['call_id']}/finish", headers=node_headers(world.nodes["home"]),
        json={"status": "delivered", "seconds": 1},
    )
    assert r.status_code == 401
    assert world.call_doc(second["call_id"])["open"] is True


def test_call_of_another_account_or_unknown_is_404(world):
    opened = world.opened()
    stranger = World(world.client, world.mongo_db, world.nodes, world.signing_key, world.app, first_agent=10)
    # A valid grant for another account's call id (as if the Cloud had signed it for the wrong account).
    caller = NodeCaller(account=stranger.account, device=stranger.watch_device, platform="watchos", node=HOME)
    token = issue_grant(
        world.signing_key, issuer="https://cloud.test", caller=caller, call_id=opened["call_id"], max_s=60,
        now=time.time(),
    )
    ghost = issue_grant(
        world.signing_key, issuer="https://cloud.test", caller=caller, call_id="c_ffffffffffffffff", max_s=60,
        now=time.time(),
    )
    for grant, call_id in ((token, opened["call_id"]), (ghost, "c_ffffffffffffffff")):
        headers = node_headers(world.nodes["home"], grant=grant)
        for method, path, body in (
            ("post", f"/v1/node/calls/{call_id}/entries", {"entries": [entry(0)]}),
            ("patch", f"/v1/node/calls/{call_id}", {"status": "processing"}),
            ("post", f"/v1/node/calls/{call_id}/finish", {"status": "delivered", "seconds": 1}),
        ):
            r = world.client.request(method, path, headers=headers, json=body)
            assert r.status_code == 404 and r.json()["error"] == "not_found", (call_id, path)
    assert world.call_doc(opened["call_id"])["open"] is True
    # Nor does a node reach a call that belongs to another node.
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": opened["call_id"]}, {"$set": {"node": "away"}})
    r = world.finish(opened, status="delivered", seconds=1)
    assert r.status_code == 404


def test_expired_grant_works_only_on_idempotent_writes(world):
    opened = world.opened()
    call_id, max_s = opened["call_id"], opened["max_s"]
    url = f"/v1/node/calls/{call_id}"
    age = max_s + GRANT_SLACK_S
    old = world.grant_for(call_id, now=time.time() - age - 25 * 3600, max_s=max_s)
    recent = world.grant_for(call_id, now=time.time() - age - 3600, max_s=max_s)
    for headers, ok in ((old, False), (recent, True)):
        results = (
            world.client.post(f"{url}/entries", headers=headers, json={"entries": [entry(0)]}),
            world.client.patch(url, headers=headers, json={"status": "processing"}),
            world.client.post(f"{url}/finish", headers=headers, json={"status": "processing", "seconds": 9}),
        )
        assert [r.status_code for r in results] == ([204] * 3 if ok else [401] * 3)
    assert world.usage()["total_s"] == 9


def test_patch_updates_the_listed_fields(world):
    opened = world.opened()
    url = f"/v1/node/calls/{opened['call_id']}"
    headers = world.node_with(opened)
    body = {
        "status": "failed", "error": "delivery_failed", "attempts": 3, "last_http_status": 502, "ended_at": 5.5,
        "finished_at": 6.5,
    }
    r = world.client.patch(url, headers=headers, json=body)
    assert r.status_code == 204 and r.content == b""
    doc = world.call_doc(opened["call_id"])
    assert {k: doc[k] for k in body} == body and doc["open"] is True and doc["updated_at"] > doc["created_at"] - 1
    r = world.client.patch(url, headers=headers, json={"finished_at": None, "error": None, "last_http_status": None})
    assert r.status_code == 204
    doc = world.call_doc(opened["call_id"])
    assert doc["finished_at"] is None and doc["error"] is None and doc["last_http_status"] is None
    assert doc["status"] == "failed" and doc["attempts"] == 3
    for bad in (
        {}, {"status": "recorded"}, {"status": None}, {"error": "Not Valid"}, {"attempts": -1}, {"attempts": True},
        {"attempts": "3"}, {"last_http_status": "502"}, {"last_http_status": 99}, {"ended_at": "x"},
        {"finished_at": "x"}, {"open": False}, {"seconds": 5}, {"account": "acc"}, {"status": "processing", "x": 1},
    ):
        assert world.client.patch(url, headers=headers, json=bad).status_code == 422, bad


def test_finish_validation(world):
    opened = world.opened()
    for bad in (
        {}, {"status": "delivered"}, {"seconds": 5}, {"status": "recording", "seconds": 5},
        {"status": "nope", "seconds": 5}, {"status": "delivered", "seconds": "5"},
        {"status": "delivered", "seconds": True}, {"status": "delivered", "seconds": None},
        {"status": "delivered", "seconds": float("nan")}, {"status": "delivered", "seconds": 5, "error": "NO"},
    ):
        assert world.finish(opened, **bad).status_code == 422, bad
    assert world.call_doc(opened["call_id"])["open"] is True


def test_finish_twice_counts_once(world):
    opened = world.opened()
    for _ in range(2):
        assert world.finish(opened, status="processing", seconds=42).status_code == 204
    usage = world.usage()
    assert usage["total_s"] == 42 and usage["seconds_by_type"] == {"one-shot": 42}
    assert usage["account"] == world.account and usage["month"] == usage_month(time.time())
    doc = world.call_doc(opened["call_id"])
    assert "open" not in doc and doc["finished_at"] is None and doc["status"] == "processing"
    assert doc["seconds"] == 42 and doc["closed_by"] == "node" and doc["ended_at"] is not None
    # A later finish with other numbers does not move anything either.
    assert world.finish(opened, status="delivered", seconds=500).status_code == 204
    assert world.usage()["total_s"] == 42 and world.call_doc(opened["call_id"])["status"] == "processing"
    assert world.open().status_code == 201


def test_finish_of_a_final_status_sets_finished_at_and_error(world):
    opened = world.opened()
    r = world.finish(opened, status="failed", error="delivery_failed", seconds=7)
    assert r.status_code == 204
    doc = world.call_doc(opened["call_id"])
    assert doc["status"] == "failed" and doc["error"] == "delivery_failed" and doc["finished_at"] == doc["ended_at"]


def test_finish_caps_seconds(world):
    opened = world.opened()
    assert world.finish(opened, status="delivered", seconds=999999).status_code == 204
    assert world.usage()["total_s"] == 1800 + 60
    assert world.call_doc(opened["call_id"])["seconds"] == 1860
    again = world.opened()
    assert world.finish(again, status="empty", seconds=-5).status_code == 204
    assert world.call_doc(again["call_id"])["seconds"] == 0 and world.usage()["total_s"] == 1860
    convo = world.opened(world.convo, "conversation")
    assert world.finish(convo, status="ended", seconds=100.5).status_code == 204
    usage = world.usage()
    assert usage["total_s"] == pytest.approx(1960.5) and usage["seconds_by_type"]["conversation"] == 100.5


def test_me_shows_usage_after_finish(world):
    opened = world.opened()
    assert world.finish(opened, status="processing", seconds=42).status_code == 204
    usage = world.client.get("/v1/node/me", headers=world.watch).json()["usage"]
    assert usage["used_s"] == 42 and usage["remaining_s"] == 35958


def test_usage_month_is_the_call_start_month(world):
    opened = world.opened()
    started = datetime(2025, 12, 31, 23, 59, 50, tzinfo=UTC).timestamp()
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": opened["call_id"]}, {"$set": {"created_at": started}})
    assert world.finish(opened, status="delivered", seconds=42).status_code == 204
    usage = world.usage("2025-12")
    assert usage["total_s"] == 42 and usage["month"] == "2025-12" and usage["account"] == world.account
    assert world.usage() is None


def closing(world, opened: dict, **kwargs):
    """`close_call` run on the app's loop (the Mongo client belongs to it), on the call as stored now."""
    config = world.app.state.config

    async def run():
        cloud = world.app.state.cloud
        call = await cloud.calls.find_one({"_id": opened["call_id"]})
        return await close_call(cloud, config, call, now=time.time(), **kwargs)

    return world.client.portal.call(run)


def test_close_call_by_the_cloud_then_a_late_finish_corrects_usage(world):
    opened = world.opened()
    orphan = dict(status="failed", error="interrupted", seconds=100, closed_by="cloud")
    assert closing(world, opened, **orphan) is True
    doc = world.call_doc(opened["call_id"])
    assert "open" not in doc and doc["status"] == "failed" and doc["error"] == "interrupted"
    assert doc["closed_by"] == "cloud" and doc["seconds"] == 100 and doc["finished_at"] is not None
    assert world.usage()["total_s"] == 100
    # The Cloud closes a call only once, whoever asks.
    assert closing(world, opened, **orphan) is False and world.usage()["total_s"] == 100
    # The node's `finish` comes late: status, error and seconds are the node's, usage moves by the difference.
    assert world.finish(opened, status="delivered", seconds=160).status_code == 204
    doc = world.call_doc(opened["call_id"])
    assert (doc["status"], doc["error"], doc["seconds"], doc["closed_by"]) == ("delivered", None, 160, "node")
    usage = world.usage()
    assert usage["total_s"] == 160 and usage["seconds_by_type"] == {"one-shot": 160}
    assert world.finish(opened, status="delivered", seconds=160).status_code == 204
    assert world.usage()["total_s"] == 160
    assert closing(world, opened, **orphan) is False


def test_racing_closers_count_a_call_once(world):
    config = world.app.state.config
    node = dict(status="delivered", error=None, seconds=60, closed_by="node")
    orphan = dict(status="failed", error="interrupted", seconds=25, closed_by="cloud")

    async def race(call_id: str) -> list[bool]:
        cloud = world.app.state.cloud
        call = await cloud.calls.find_one({"_id": call_id})
        now = time.time()
        return await asyncio.gather(
            *(close_call(cloud, config, call, now=now, **kw) for kw in (node, orphan, node, orphan))
        )

    for _ in range(5):
        opened = world.opened()
        results = world.client.portal.call(race, opened["call_id"])
        assert any(results)
        # Whoever won, the call counts what the node said: the cloud's estimate is corrected, never added on top.
        doc = world.call_doc(opened["call_id"])
        assert doc["closed_by"] == "node" and doc["seconds"] == 60 and "open" not in doc
    assert world.usage()["total_s"] == 5 * 60
