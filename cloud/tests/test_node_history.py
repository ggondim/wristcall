"""History of the Cloud nodes: listing and search by term hash, reading, NDJSON export, deleting and redelivery
(`/v1/node/calls*`, `node_history.py`). Calls and entries are made by the routes of `node_calls.py`."""

import json
import time

import jwt

from node_fixtures import (
    NODE_A,
    NODE_B,
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
from test_node_calls import HASH_A, HASH_B, World, entry, world
from wristcall_cloud.node_auth import verify_grant
from wristcall_cloud.node_history import router as history_router

HASH_C = "x" + "aaaaaaaaaaaaaaaa"
BASE = 1_700_000_000.0


def make_call(
    world: World, *, agent: str | None = None, call_type: str = "one-shot", created_at: float | None = None,
    status: str = "delivered", error: str | None = None, hashes: list[str] | None = None, seconds: int = 5,
    headers: dict | None = None, entries: int = 2, finish: bool = True,
) -> str:
    """Opens a call through the route, writes `entries` entries, finishes it, and (with `created_at`) places it in
    time; returns the call id."""
    opened = world.opened(agent, call_type, headers=headers)
    call_id = opened["call_id"]
    items = [entry(n, term_hashes=hashes if hashes is not None else [HASH_B]) for n in range(entries)]
    r = world.client.post(f"/v1/node/calls/{call_id}/entries", headers=world.node_with(opened), json={"entries": items})
    assert r.status_code == 204, r.text
    if finish:
        r = world.client.post(
            f"/v1/node/calls/{call_id}/finish", headers=world.node_with(opened),
            json={"status": status, "error": error, "seconds": seconds},
        )
        assert r.status_code == 204, r.text
    if created_at is not None:
        with db(world.mongo_db) as d:
            d["cloud_calls"].update_one({"_id": call_id}, {"$set": {"created_at": created_at}})
            d["cloud_call_entries"].update_many({"call": call_id}, {"$set": {"call_created_at": created_at}})
    return call_id


def history(world: World, headers: dict | None = None, **params):
    return world.client.get("/v1/node/calls", headers=headers or world.user(world.ios_device, "ios"), params=params)


def ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [c["id"] for c in response.json()["calls"]]


def ios(world: World) -> dict:
    return world.user(world.ios_device, "ios")


def test_list_pages_newest_first(world):
    made = [make_call(world, created_at=BASE + n) for n in range(5)]
    # Two calls at the same instant: `_id` breaks the tie, so a page boundary between them loses neither.
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": made[3]}, {"$set": {"created_at": BASE + 2}})
    expected = sorted(made, key=lambda i: (world.call_doc(i)["created_at"], i), reverse=True)
    seen: list[str] = []
    params: dict = {"limit": 2}
    while True:
        r = history(world, **params)
        calls = r.json()["calls"]
        assert len(calls) <= 2
        if not calls:
            break
        seen += [c["id"] for c in calls]
        params = {"limit": 2, "before_at": calls[-1]["created_at"], "before_id": calls[-1]["id"]}
    assert seen == expected
    assert ids(history(world)) == expected
    shape = history(world, limit=1).json()["calls"][0]
    assert set(shape) == {
        "id", "agent_id", "agent_slug", "agent_name", "device_id", "call_type", "status", "error", "attempts",
        "last_http_status", "delivery", "created_at", "updated_at", "ended_at", "finished_at", "expires_at", "seconds",
            "open",
    }
    assert shape["open"] is False and shape["agent_id"] == world.oneshot and shape["device_id"] == world.watch_device


def test_list_filters_and_limits(world):
    old = make_call(world, created_at=BASE)
    mid = make_call(world, agent=world.convo, call_type="conversation", created_at=BASE + 10, status="ended")
    new = make_call(world, created_at=BASE + 20)
    assert ids(history(world, agent=world.convo)) == [mid]
    assert ids(history(world, since=BASE + 10)) == [new, mid]
    assert ids(history(world, until=BASE + 10)) == [old]  # `until` is exclusive
    assert ids(history(world, since=BASE + 5, until=BASE + 20)) == [mid]
    assert ids(history(world, agent=world.oneshot, since=BASE + 1)) == [new]
    assert ids(history(world, agent="ag_000000000000")) == []
    for bad in (
        {"limit": 0}, {"limit": 201}, {"limit": "x"}, {"limit": -1}, {"since": "x"}, {"until": "nan"},
        {"since": "inf"}, {"before_at": BASE}, {"before_id": old}, {"agent": ""}, {"agent": "x" * 65},
    ):
        assert history(world, **bad).status_code == 422, bad
    assert history(world, limit=200).status_code == 200
    r = world.client.get("/v1/node/calls?limit=1&limit=2", headers=ios(world))
    assert r.status_code == 422


def test_list_needs_a_management_platform(world):
    make_call(world)
    for url in ("/v1/node/calls", "/v1/node/calls/export"):
        assert world.client.get(url, headers=world.watch).status_code == 403
    assert world.client.get("/v1/node/calls").status_code == 401


def test_search_by_term_hashes(world):
    a = make_call(world, created_at=BASE, hashes=[HASH_A, HASH_B])
    b = make_call(world, created_at=BASE + 1, hashes=[HASH_B])
    c = make_call(world, created_at=BASE + 2, hashes=[HASH_C], agent=world.convo, call_type="conversation",
                  status="ended")
    get = world.client.get
    url = "/v1/node/calls"
    assert ids(get(url, headers=ios(world), params={"terms": HASH_B})) == [b, a]
    # Two `terms` are two groups: the call must be in both.
    assert ids(get(url, headers=ios(world), params=[("terms", HASH_A), ("terms", HASH_B)])) == [a]
    # Hashes inside one group are alternatives.
    assert ids(get(url, headers=ios(world), params={"terms": f"{HASH_A},{HASH_C}"})) == [c, a]
    assert ids(get(url, headers=ios(world), params=[("terms", HASH_A), ("terms", HASH_C)])) == []
    assert ids(get(url, headers=ios(world), params={"terms": HASH_B, "agent": world.convo})) == []
    assert ids(get(url, headers=ios(world), params={"terms": HASH_B, "limit": 1})) == [b]
    assert ids(get(url, headers=ios(world), params={"terms": "x" + "1" * 16})) == []
    for bad in (
        "plain", "x123", HASH_A.upper(), f"{HASH_A},", f"{HASH_A},nope", "",
        ",".join(f"x{n:016x}" for n in range(17)),
    ):
        r = get(url, headers=ios(world), params={"terms": bad})
        assert r.status_code == 422, bad
        assert not bad or bad not in r.text
    sixteen = [("terms", f"x{n:016x}") for n in range(16)]
    assert get(url, headers=ios(world), params=sixteen).status_code == 200
    assert get(url, headers=ios(world), params=[*sixteen, ("terms", HASH_A)]).status_code == 422
    assert get(url, headers=ios(world), params={"terms": ",".join(f"x{n:016x}" for n in range(16))}).status_code == 200


def test_read_one_call(world):
    call_id = make_call(world, entries=3)
    r = world.client.get(f"/v1/node/calls/{call_id}", headers=ios(world))
    assert r.status_code == 200
    body = r.json()
    assert body["call"]["id"] == call_id and body["call"]["status"] == "delivered"
    assert [e["seq"] for e in body["entries"]] == [0, 1, 2]
    assert body["entries"][0] == {
        "seq": 0, "role": "user", "text": "sealed-0", "sealed": True, "error": None, "at": 1000.5,
    }
    # A watch reads its own calls only.
    assert world.client.get(f"/v1/node/calls/{call_id}", headers=world.watch).status_code == 200
    other = make_call(world, headers=ios(world))
    assert world.client.get(f"/v1/node/calls/{other}", headers=world.watch).status_code == 404
    assert world.client.get(f"/v1/node/calls/{other}", headers=ios(world)).status_code == 200
    assert world.client.get("/v1/node/calls/c_0000000000000000", headers=ios(world)).status_code == 404


def test_export_streams_ndjson(world):
    new = make_call(world, created_at=BASE + 5, entries=1)
    old = make_call(world, created_at=BASE, entries=2)
    r = world.client.get("/v1/node/calls/export", headers=ios(world))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in r.text.split("\n") if line]
    assert r.text.endswith("\n") and len(lines) == 3
    assert [line["call"]["id"] for line in lines[:2]] == [old, new]
    assert [len(line["entries"]) for line in lines[:2]] == [2, 1]
    assert lines[0]["entries"][1]["seq"] == 1 and lines[0]["call"]["status"] == "delivered"
    assert lines[2] == {"end": True, "count": 2}
    # Filters narrow the export; an empty one still ends with the end line.
    r = world.client.get("/v1/node/calls/export", headers=ios(world), params={"since": BASE + 1})
    assert [json.loads(x) for x in r.text.split("\n") if x][0]["call"]["id"] == new
    r = world.client.get("/v1/node/calls/export", headers=ios(world), params={"agent": world.convo})
    assert [json.loads(x) for x in r.text.split("\n") if x] == [{"end": True, "count": 0}]
    assert world.client.get("/v1/node/calls/export", headers=ios(world), params={"since": "x"}).status_code == 422


def test_old_call_without_delivery_reads_null(world):
    """A call document written before the field existed (cloud 0.5.0) reads `delivery: null` everywhere."""
    call_id = make_call(world, entries=1)
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": call_id}, {"$unset": {"delivery": ""}})
        assert "delivery" not in d["cloud_calls"].find_one({"_id": call_id})
    listed = world.client.get("/v1/node/calls", headers=ios(world)).json()["calls"]
    assert [c["delivery"] for c in listed if c["id"] == call_id] == [None]
    r = world.client.get(f"/v1/node/calls/{call_id}", headers=ios(world))
    assert r.status_code == 200 and r.json()["call"]["delivery"] is None
    r = world.client.get("/v1/node/calls/export", headers=ios(world))
    lines = [json.loads(line) for line in r.text.split("\n") if line]
    assert lines[0]["call"]["id"] == call_id and lines[0]["call"]["delivery"] is None


def test_export_is_declared_before_the_call_route(world):
    """`/calls/export` would be read as the id `export` if `/calls/{call_id}` came first; the export answers over
    HTTP too (not a 404 of the call route), and the routes the lifecycle owns are still reachable."""
    gets = [r.path for r in history_router.routes if "GET" in r.methods]
    assert gets.index("/v1/node/calls/export") < gets.index("/v1/node/calls/{call_id}")
    assert world.client.get("/v1/node/calls/export", headers=ios(world)).headers["content-type"].startswith(
        "application/x-ndjson"
    )
    opened = world.opened()
    url = f"/v1/node/calls/{opened['call_id']}"
    assert world.client.patch(url, headers=world.node_with(opened), json={"attempts": 1}).status_code == 204
    assert world.client.get(url, headers=ios(world)).status_code == 200
    assert world.client.post("/v1/node/calls", headers=world.watch, json={}).status_code == 422
    assert world.client.delete("/v1/node/calls", headers=ios(world)).status_code == 422


def test_delete_one_call(world):
    call_id = make_call(world)
    keep = make_call(world)
    assert world.client.delete(f"/v1/node/calls/{call_id}", headers=world.watch).status_code == 403
    r = world.client.delete(f"/v1/node/calls/{call_id}", headers=ios(world))
    assert r.status_code == 204 and r.content == b""
    assert world.call_doc(call_id) is None
    with db(world.mongo_db) as d:
        assert d["cloud_call_entries"].count_documents({"call": call_id}) == 0
        assert d["cloud_call_entries"].count_documents({"call": keep}) == 2
    assert world.client.delete(f"/v1/node/calls/{call_id}", headers=ios(world)).status_code == 404
    assert world.client.get(f"/v1/node/calls/{call_id}", headers=ios(world)).status_code == 404


def test_delete_open_call_is_409(world):
    opened = world.opened()
    r = world.client.delete(f"/v1/node/calls/{opened['call_id']}", headers=ios(world))
    assert r.status_code == 409 and r.json()["error"] == "call_open"
    assert world.call_doc(opened["call_id"]) is not None


def test_delete_all_needs_agent_or_all(world):
    make_call(world)
    for url in ("/v1/node/calls", "/v1/node/calls?all=false", "/v1/node/calls?all=1"):
        r = world.client.delete(url, headers=ios(world))
        assert r.status_code == 422 and r.json()["error"] == "invalid", url
    assert world.client.delete("/v1/node/calls?all=true", headers=world.watch).status_code == 403
    assert len(ids(history(world))) == 1


def test_delete_by_agent_and_all(world):
    one = make_call(world)
    two = make_call(world)
    convo = make_call(world, agent=world.convo, call_type="conversation", status="ended")
    r = world.client.delete(f"/v1/node/calls?agent={world.oneshot}", headers=ios(world))
    assert r.status_code == 200 and r.json() == {"deleted": 2}
    assert ids(history(world)) == [convo]
    with db(world.mongo_db) as d:
        assert d["cloud_call_entries"].count_documents({"call": {"$in": [one, two]}}) == 0
        assert d["cloud_call_entries"].count_documents({"call": convo}) == 2
    r = world.client.delete("/v1/node/calls?all=true", headers=ios(world))
    assert r.json() == {"deleted": 1} and ids(history(world)) == []
    assert world.client.delete("/v1/node/calls?all=true", headers=ios(world)).json() == {"deleted": 0}


def test_delete_all_keeps_open_calls(world):
    done = make_call(world)
    opened = world.opened()
    r = world.client.delete("/v1/node/calls?all=true", headers=ios(world))
    assert r.json() == {"deleted": 1}
    assert world.call_doc(done) is None and world.call_doc(opened["call_id"]) is not None
    # The open call still closes and its usage still counts.
    assert world.finish(opened, status="delivered", seconds=40).status_code == 204
    assert world.usage()["total_s"] == 45
    assert ids(history(world)) == [opened["call_id"]]


def failed_one_way(world: World, error: str = "delivery_failed", attempts: int = 3) -> str:
    """A one-way call the node gave up delivering: closed by `finish` (still `processing`), then marked failed."""
    opened = world.opened()
    call_id = opened["call_id"]
    headers = world.node_with(opened)
    r = world.client.post(
        f"/v1/node/calls/{call_id}/entries", headers=headers, json={"entries": [entry(0), entry(1, role="agent")]}
    )
    assert r.status_code == 204
    assert world.finish(opened, status="processing", seconds=12).status_code == 204
    patch = {"status": "failed", "error": error, "attempts": attempts, "last_http_status": 502, "finished_at": BASE}
    assert world.client.patch(f"/v1/node/calls/{call_id}", headers=headers, json=patch).status_code == 204
    return call_id


def test_redeliver_leaves_delivery_unchanged(world):
    call_id = failed_one_way(world)
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": call_id}, {"$set": {"delivery": "webhook"}})
    r = world.client.post(
        f"/v1/node/calls/{call_id}/redeliver", headers=world.user(world.ios_device, "ios", "away", NODE_B)
    )
    assert r.status_code == 200, r.text
    assert r.json()["call"]["delivery"] == "webhook" and world.call_doc(call_id)["delivery"] == "webhook"


def test_redeliver_failed_one_way(world):
    call_id = failed_one_way(world)
    # `away` asks, as the manager's iPhone: the call becomes the node's, and the grant is for `away` only.
    headers = world.user(world.ios_device, "ios", "away", NODE_B)
    before = time.time()
    r = world.client.post(f"/v1/node/calls/{call_id}/redeliver", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"call", "entries", "agent", "grant"}
    assert body["call"]["id"] == call_id and body["call"]["status"] == "processing"
    assert body["call"]["error"] is None and body["call"]["finished_at"] is None and body["call"]["attempts"] == 3
    assert [e["seq"] for e in body["entries"]] == [0, 1]
    assert body["agent"]["id"] == world.oneshot and body["agent"]["spec"]["retention_days"] == 30
    doc = world.call_doc(call_id)
    assert doc["node"] == "away" and doc["status"] == "processing" and doc["attempts"] == 3
    assert doc["updated_at"] >= before and not doc.get("open")
    away = {"_id": "away", "url": NODE_B}
    request = type("R", (), {"app": world.app})()
    grant = verify_grant(request, away, call_id, body["grant"], allow_expired=False)
    assert grant.account == world.account and grant.node_id == "away" and grant.call_id == call_id
    # The grant lives `max_one_way_s` plus the slack, and `home` no longer reaches the call.
    claims = jwt.decode(body["grant"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == world.app.state.config.max_one_way_s + 3600
    r = world.client.patch(
        f"/v1/node/calls/{call_id}", headers=node_headers(world.nodes["away"], grant=body["grant"]),
        json={"status": "delivered", "error": None, "finished_at": time.time()},
    )
    assert r.status_code == 204
    assert world.call_doc(call_id)["status"] == "delivered"
    # Once it is processing, a second request finds it busy.
    again = world.client.post(f"/v1/node/calls/{call_id}/redeliver", headers=headers)
    assert again.status_code == 409


def test_redeliver_after_cloud_interrupt(world):
    call_id = failed_one_way(world, error="interrupted", attempts=0)
    r = world.client.post(f"/v1/node/calls/{call_id}/redeliver", headers=ios(world))
    assert r.status_code == 200 and r.json()["call"]["attempts"] == 0


def test_redeliver_after_cloud_interrupt_then_zero_finish_keeps_usage(world):
    call_id = failed_one_way(world, error="interrupted", attempts=0)
    with db(world.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": call_id}, {"$set": {"closed_by": "cloud"}})
    seconds, total = world.call_doc(call_id)["seconds"], world.usage()["total_s"]
    assert seconds == 12 and total == 12
    r = world.client.post(f"/v1/node/calls/{call_id}/redeliver", headers=ios(world))
    assert r.status_code == 200, r.text
    assert world.call_doc(call_id)["closed_by"] == "node"
    opened = {"call_id": call_id, "grant": r.json()["grant"]}
    assert world.finish(opened, status="delivered", error=None, seconds=0).status_code == 204
    doc = world.call_doc(call_id)
    assert doc["seconds"] == seconds and world.usage()["total_s"] == total


def test_redeliver_without_its_agent(world):
    call_id = failed_one_way(world)
    with db(world.mongo_db) as d:
        d["cloud_agents"].delete_one({"_id": world.oneshot})
    r = world.client.post(f"/v1/node/calls/{call_id}/redeliver", headers=ios(world))
    assert r.status_code == 200 and r.json()["agent"] is None


def test_redeliver_refusals(world):
    # A conversation is not delivered.
    convo = make_call(world, agent=world.convo, call_type="conversation", status="ended")
    r = world.client.post(f"/v1/node/calls/{convo}/redeliver", headers=ios(world))
    assert r.status_code == 409 and r.json()["error"] == "not_one_way"
    # Being delivered right now.
    busy = make_call(world, status="processing")
    r = world.client.post(f"/v1/node/calls/{busy}/redeliver", headers=ios(world))
    assert r.status_code == 409 and r.json()["error"] == "busy"
    # Failed for a reason a second delivery does not mend.
    stt = failed_one_way(world, error="stt_failed")
    r = world.client.post(f"/v1/node/calls/{stt}/redeliver", headers=ios(world))
    assert r.status_code == 409 and r.json()["error"] == "not_failed"
    assert world.call_doc(stt)["status"] == "failed" and world.call_doc(stt)["node"] == "home"
    done = make_call(world)
    r = world.client.post(f"/v1/node/calls/{done}/redeliver", headers=ios(world))
    assert r.status_code == 409 and r.json()["error"] == "not_failed"
    # Open.
    opened = world.opened()
    r = world.client.post(f"/v1/node/calls/{opened['call_id']}/redeliver", headers=ios(world))
    assert r.status_code == 409 and r.json()["error"] == "call_open"
    # Not found, and not for a watch.
    assert world.client.post("/v1/node/calls/c_0000000000000000/redeliver", headers=ios(world)).status_code == 404
    assert world.client.post(f"/v1/node/calls/{stt}/redeliver", headers=world.watch).status_code == 403


def test_calls_of_another_account_are_invisible(world, node_client, mongo_db, signing_key, nodes):
    mine = failed_one_way(world)
    done = make_call(world, hashes=[HASH_A])
    account, device, key = new_account(node_client, "ios")
    grant_plan(mongo_db, account)
    theirs = node_headers(nodes["home"], user=user_token(signing_key, account, device, "ios", NODE_A))
    assert ids(node_client.get("/v1/node/calls", headers=theirs)) == []
    assert ids(node_client.get("/v1/node/calls", headers=theirs, params={"terms": HASH_A})) == []
    export = node_client.get("/v1/node/calls/export", headers=theirs)
    assert [json.loads(x) for x in export.text.split("\n") if x] == [{"end": True, "count": 0}]
    for call_id in (mine, done):
        assert node_client.get(f"/v1/node/calls/{call_id}", headers=theirs).status_code == 404
        assert node_client.delete(f"/v1/node/calls/{call_id}", headers=theirs).status_code == 404
        assert node_client.post(f"/v1/node/calls/{call_id}/redeliver", headers=theirs).status_code == 404
    assert node_client.delete("/v1/node/calls?all=true", headers=theirs).json() == {"deleted": 0}
    assert world.call_doc(mine)["status"] == "failed" and world.call_doc(mine)["node"] == "home"
    assert world.call_doc(done) is not None
    with db(mongo_db) as d:
        assert d["cloud_call_entries"].count_documents({"call": done}) == 2

