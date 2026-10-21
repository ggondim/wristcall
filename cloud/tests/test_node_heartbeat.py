"""Node heartbeat (`POST /v1/node/heartbeat`): its state, orphan calls closed by the Cloud, the periodic sweeper with
the Cloud-restart tolerance, and `processing` calls failed when a node restarts or goes silent."""

import asyncio
import time

import pytest

from node_fixtures import (
    db,
    node_app,
    node_client,
    node_config,
    node_headers,
    nodes,
    signing_key,
)
from test_node_calls import World, world
from wristcall_cloud import node_heartbeat
from wristcall_cloud.node_heartbeat import orphan_sweep_loop, sweep_orphans


def body(**over) -> dict:
    return {
        "boot_id": "boot-1", "version": "0.9.0", "active_call_ids": [],
        "active_by_type": {"conversation": 0, "one_way": 0}, "capacity": {"conversation": None, "one_way": 2},
        "providers_ok": True, **over,
    }


def beat(world: World, node: str = "home", **over):
    return world.client.post("/v1/node/heartbeat", headers=node_headers(world.nodes[node]), json=body(**over))


def sweep(world: World, now: float) -> int:
    async def run() -> int:
        return await sweep_orphans(world.app.state, now=now)

    return world.client.portal.call(run)


def age_call(world: World, call_id: str, created_at: float) -> None:
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": call_id}, {"$set": {"created_at": created_at}})


def set_node(world: World, node: str = "home", **fields) -> None:
    with db(world.mongo_db) as d:
        d["nodes"].update_one({"_id": node}, {"$set": fields})


def node_doc(world: World, node: str = "home") -> dict:
    with db(world.mongo_db) as d:
        return d["nodes"].find_one({"_id": node})


def second_world(world: World) -> World:
    """Another account (own agents) on the same Cloud, so that both can have an open call."""
    return World(world.client, world.mongo_db, world.nodes, world.signing_key, world.app, first_agent=10)


def finish_on(world: World, opened: dict, node: str, **fields):
    return world.client.post(
        f"/v1/node/calls/{opened['call_id']}/finish", headers=world.node_with(opened, node), json=fields
    )


def test_heartbeat_needs_the_node_credential(world):
    assert world.client.post("/v1/node/heartbeat", json=body()).status_code == 401
    assert world.client.post("/v1/node/heartbeat", headers=world.watch, json=body()).status_code == 204
    assert world.client.get("/v1/node/heartbeat", headers=node_headers(world.nodes["home"])).status_code == 405
    bad = {"Authorization": "Bearer wc_node_" + "x" * 43}
    assert world.client.post("/v1/node/heartbeat", headers=bad, json=body()).status_code == 401


@pytest.mark.parametrize(
    "over",
    [
        {"boot_id": ""}, {"boot_id": "b" * 65}, {"boot_id": 1}, {"version": ""}, {"version": "v" * 33},
        {"active_call_ids": "c_1"}, {"active_call_ids": [1]}, {"active_call_ids": [f"c_{n}" for n in range(501)]},
        {"active_by_type": {"conversation": -1, "one_way": 0}}, {"active_by_type": {"conversation": 0}},
        {"active_by_type": {"conversation": True, "one_way": 0}}, {"active_by_type": None},
        {"capacity": {"conversation": 1.5, "one_way": 0}}, {"capacity": {"conversation": -1, "one_way": None}},
        {"capacity": {"one_way": 1}}, {"capacity": 3}, {"providers_ok": "yes"}, {"providers_ok": None},
    ],
)
def test_heartbeat_fields_are_validated(world, over):
    assert beat(world, **over).status_code == 422
    assert "last_seen_at" not in node_doc(world)


def test_heartbeat_body_shapes_are_422(world):
    headers = {**node_headers(world.nodes["home"]), "Content-Type": "application/json"}
    for raw_body in (b"[]", b"null", b"nope", b"{}"):
        assert world.client.post("/v1/node/heartbeat", headers=headers, content=raw_body).status_code == 422


def test_heartbeat_stores_the_node_state(world):
    first = time.time()
    assert beat(world, active_by_type={"conversation": 1, "one_way": 2}, capacity={"conversation": 3, "one_way": None},
                providers_ok=False).status_code == 204
    doc = node_doc(world)
    assert first <= doc["last_seen_at"] <= time.time() and "prev_seen_at" not in doc
    assert doc["boot_id"] == "boot-1" and doc["version"] == "0.9.0" and doc["providers_ok"] is False
    assert doc["active_by_type"] == {"conversation": 1, "one_way": 2}
    assert doc["capacity"] == {"conversation": 3, "one_way": None}
    seen = doc["last_seen_at"]
    assert beat(world, boot_id="boot-1", version="0.9.1").status_code == 204
    doc = node_doc(world)
    assert doc["prev_seen_at"] == seen and doc["last_seen_at"] >= seen and doc["version"] == "0.9.1"
    assert "last_seen_at" not in node_doc(world, "away")


def test_heartbeat_closes_only_its_own_node_calls(world):
    other = second_world(world)
    now = time.time()
    mine = world.opened()["call_id"]
    away = other.opened(headers=other.watch_away)["call_id"]
    for call_id in (mine, away):
        age_call(world, call_id, now - 100)
    set_node(world, "home", last_seen_at=now - 90)
    assert beat(world).status_code == 204
    doc = world.call_doc(mine)
    assert "open" not in doc and doc["status"] == "failed" and doc["error"] == "interrupted"
    assert doc["closed_by"] == "cloud" and doc["ended_at"] is not None
    # Until the previous heartbeat: 10 s after the call began.
    assert doc["seconds"] == 10 and world.usage()["total_s"] == 10
    assert world.call_doc(away)["open"] is True and other.usage() is None


def test_heartbeat_keeps_listed_and_recent_calls(world):
    now = time.time()
    listed = world.opened()
    age_call(world, listed["call_id"], now - 100)
    assert beat(world, active_call_ids=[listed["call_id"]]).status_code == 204
    assert world.call_doc(listed["call_id"])["open"] is True
    # Not listed, but inside the grace period (the node may not have reported it yet).
    age_call(world, listed["call_id"], now - 5)
    assert beat(world).status_code == 204
    assert world.call_doc(listed["call_id"])["open"] is True
    # Grace gone: closed; a conversation ends, it does not fail.
    age_call(world, listed["call_id"], now - 100)
    assert beat(world).status_code == 204
    assert world.call_doc(listed["call_id"])["status"] == "failed"
    convo = world.opened(world.convo, "conversation")["call_id"]
    age_call(world, convo, now - 100)
    assert beat(world).status_code == 204
    doc = world.call_doc(convo)
    assert doc["status"] == "ended" and doc["error"] == "interrupted" and "open" not in doc


def test_heartbeat_first_beat_counts_until_now(world):
    opened = world.opened()
    age_call(world, opened["call_id"], time.time() - 100)
    assert beat(world).status_code == 204
    assert 99 <= world.call_doc(opened["call_id"])["seconds"] <= 102
    assert world.open().status_code == 201


def test_heartbeat_completes_a_call_claimed_and_never_freed(world):
    opened = world.opened()
    call_id = opened["call_id"]
    age_call(world, call_id, time.time() - 100)
    with db(world.mongo_db) as d:  # a closer that stopped after writing its claim
        d["cloud_calls"].update_one(
            {"_id": call_id}, {"$set": {"closed_by": "cloud", "status": "failed", "error": "interrupted", "seconds": 7}}
        )
    set_node(world, "home", last_seen_at=time.time() - 1)
    assert beat(world).status_code == 204
    assert "open" not in world.call_doc(call_id) and world.usage()["total_s"] == 7


def test_new_boot_fails_processing_calls(world):
    other = second_world(world)
    assert beat(world, boot_id="boot-1").status_code == 204
    assert beat(world, "away", boot_id="boot-9").status_code == 204
    mine = world.opened()
    assert finish_on(world, mine, "home", status="processing", seconds=5).status_code == 204
    away = other.opened(headers=other.watch_away)
    assert finish_on(other, away, "away", status="processing", seconds=5).status_code == 204
    # Same boot: nothing happens to a call that is processing.
    assert beat(world, boot_id="boot-1").status_code == 204
    assert world.call_doc(mine["call_id"])["status"] == "processing"
    before = time.time()
    assert beat(world, boot_id="boot-2").status_code == 204
    doc = world.call_doc(mine["call_id"])
    assert doc["status"] == "failed" and doc["error"] == "interrupted" and doc["finished_at"] >= before
    # Still the node's call (`closed_by` stays): a late `finish` cannot correct it (the call is closed), but a late
    # `PATCH` of the status can.
    assert doc["closed_by"] == "node" and doc["updated_at"] >= before
    patched = world.client.patch(
        f"/v1/node/calls/{mine['call_id']}", headers=world.node_with(mine), json={"status": "delivered"}
    )
    assert patched.status_code == 204
    assert world.call_doc(mine["call_id"])["status"] == "delivered"
    assert other.call_doc(away["call_id"])["status"] == "processing"
    assert node_doc(world)["boot_id"] == "boot-2"


def test_new_boot_frees_and_fails_a_call_a_crashed_finish_left_open(world):
    opened = world.opened()
    call_id = opened["call_id"]
    assert beat(world, boot_id="boot-1").status_code == 204
    age_call(world, call_id, time.time() - 100)
    with db(world.mongo_db) as d:  # `finish` claimed the call as processing and the Cloud crashed before freeing it
        d["cloud_calls"].update_one(
            {"_id": call_id}, {"$set": {"closed_by": "node", "status": "processing", "seconds": 9}}
        )
    assert beat(world, boot_id="boot-2").status_code == 204
    doc = world.call_doc(call_id)
    assert "open" not in doc and doc["status"] == "failed" and doc["error"] == "interrupted"
    assert doc["closed_by"] == "node" and world.usage()["total_s"] == 9
    assert world.open().status_code == 201


def test_finish_after_cloud_close_corrects_usage(world):
    opened = world.opened()
    call_id = opened["call_id"]
    now = time.time()
    age_call(world, call_id, now - 100)
    set_node(world, "home", last_seen_at=now - 90)
    assert beat(world).status_code == 204
    doc = world.call_doc(call_id)
    assert doc["closed_by"] == "cloud" and doc["status"] == "failed" and doc["seconds"] == 10
    assert world.usage()["total_s"] == 10
    for _ in range(2):
        assert world.finish(opened, status="processing", seconds=50).status_code == 204
        doc = world.call_doc(call_id)
        assert doc["status"] == "processing" and doc["error"] is None and doc["seconds"] == 50
        assert doc["closed_by"] == "node" and world.usage()["total_s"] == 50


def test_sweeper_closes_calls_of_silent_node(world):
    now = time.time()
    world.app.state.started_at = now - 3600
    opened = world.opened()
    age_call(world, opened["call_id"], now - 500)
    set_node(world, "home", last_seen_at=now - 200)
    # Seen 200 s ago: past `node_stale_s` (120 s).
    assert sweep(world, now) == 1
    doc = world.call_doc(opened["call_id"])
    assert "open" not in doc and doc["status"] == "failed" and doc["error"] == "interrupted"
    assert doc["closed_by"] == "cloud" and doc["seconds"] == 300 and world.usage()["total_s"] == 300
    assert sweep(world, now) == 0
    assert world.open().status_code == 201


def test_sweeper_leaves_a_node_that_is_alive(world):
    now = time.time()
    world.app.state.started_at = now - 3600
    opened = world.opened()
    age_call(world, opened["call_id"], now - 500)
    set_node(world, "home", last_seen_at=now - 100)
    assert sweep(world, now) == 0
    assert world.call_doc(opened["call_id"])["open"] is True


def test_sweeper_closes_a_call_of_a_node_never_seen(world):
    now = time.time()
    world.app.state.started_at = now - 3600
    opened = world.opened(world.convo, "conversation")
    age_call(world, opened["call_id"], now - 500)
    assert sweep(world, now) == 1
    doc = world.call_doc(opened["call_id"])
    assert doc["status"] == "ended" and doc["seconds"] == 0 and "open" not in doc


def test_sweeper_waits_stale_s_after_cloud_start(world):
    now = time.time()
    opened = world.opened()
    age_call(world, opened["call_id"], now - 4000)
    set_node(world, "home", last_seen_at=now - 3600)
    # The Cloud came up 60 s ago: the node had no one to talk to until then.
    world.app.state.started_at = now - 60
    assert sweep(world, now) == 0
    assert world.call_doc(opened["call_id"])["open"] is True
    world.app.state.started_at = now - 200
    assert sweep(world, now) == 1
    doc = world.call_doc(opened["call_id"])
    assert "open" not in doc and doc["seconds"] == 400  # up to the last heartbeat, not to `now`


def test_sweeper_fails_processing_calls_of_silent_node(world):
    other = second_world(world)
    now = time.time()
    world.app.state.started_at = now - 3600
    mine = world.opened()
    assert finish_on(world, mine, "home", status="processing", seconds=5).status_code == 204
    away = other.opened(headers=other.watch_away)
    assert finish_on(other, away, "away", status="processing", seconds=5).status_code == 204
    set_node(world, "home", last_seen_at=now - 200)
    set_node(world, "away", last_seen_at=now - 10)
    assert sweep(world, now) == 1
    doc = world.call_doc(mine["call_id"])
    assert doc["status"] == "failed" and doc["error"] == "interrupted" and doc["finished_at"] == now
    assert doc["closed_by"] == "node"
    assert other.call_doc(away["call_id"])["status"] == "processing"
    assert sweep(world, now) == 0


def test_sweeper_completes_a_claimed_call_still_open(world):
    now = time.time()
    world.app.state.started_at = now - 3600
    opened = world.opened()
    age_call(world, opened["call_id"], now - 500)
    set_node(world, "home", last_seen_at=now - 200)
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one(
            {"_id": opened["call_id"]},
            {"$set": {"closed_by": "cloud", "status": "failed", "error": "interrupted", "seconds": 11}},
        )
    assert sweep(world, now) == 1
    assert "open" not in world.call_doc(opened["call_id"]) and world.usage()["total_s"] == 11


def test_orphan_sweep_loop_survives_a_failed_round(monkeypatch):
    rounds, slept = [], []

    async def fake_sweep(state, *, now):
        rounds.append(now)
        if len(rounds) == 1:
            raise RuntimeError("mongo went away")
        return 2

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(node_heartbeat, "sweep_orphans", fake_sweep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orphan_sweep_loop(object(), every_s=30, sleep=sleep, now=lambda: 123.0))
    assert slept == [30, 30, 30] and rounds == [123.0, 123.0]
