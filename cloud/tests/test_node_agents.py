"""Cloud-mode agents for nodes: `/v1/node/agents*` (validation, ETag, platform and plan rules, order, retention)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from node_fixtures import (
    NODE_A,
    add_device,
    db,
    grant_plan,
    new_account,
    node_app,
    node_client,
    node_config,
    node_headers,
    nodes,
    register_nodes,
    served_node,
    signing_key,
    user_token,
)

from wristcall_cloud.node_agents import agents_etag


def caller(client, mongo_db, nodes, signing_key, *, platform="ios", plan="full"):
    """A new account with one device; returns (account, headers a node would send for it)."""
    account, device, key = new_account(client, "ios")
    if platform != "ios":
        device = add_device(client, key, platform)
    if plan:
        grant_plan(mongo_db, account, plan)
    token = user_token(signing_key, account, device, platform, NODE_A)
    return account, node_headers(nodes["home"], user=token)


def agent_body(n: int = 1, **over):
    body = {
        "id": f"ag_{n:012x}",
        "slug": f"agent-{n}",
        "display_name": f"Agent {n}",
        "icon": "waveform",
        "color": "blue",
        "call_type": "conversation",
        "spec": {"stt": {"sealed": "AAAA"}, "retention_days": 30},
    }
    body.update(over)
    return body


def create(client, headers, n: int = 1, **over):
    r = client.post("/v1/node/agents", headers=headers, json=agent_body(n, **over))
    assert r.status_code == 201, r.text
    return r.json()


def test_create_list_get_patch_delete_agent(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    created = create(node_client, headers)
    assert created["id"] == "ag_000000000001" and created["slug"] == "agent-1"
    assert created["position"] == 0 and created["spec"]["retention_days"] == 30
    assert set(created) == {
        "id", "slug", "display_name", "icon", "color", "call_type", "position", "spec", "created_at", "updated_at"
    }

    listed = node_client.get("/v1/node/agents", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert [a["id"] for a in body["agents"]] == [created["id"]]
    assert listed.headers["ETag"] == body["etag"]
    assert body["etag"].startswith('"') and body["etag"].endswith('"') and len(body["etag"]) == 34

    same = node_client.get("/v1/node/agents", headers={**headers, "If-None-Match": body["etag"]})
    assert same.status_code == 304 and same.content == b""

    got = node_client.get(f"/v1/node/agents/{created['id']}", headers=headers)
    assert got.status_code == 200 and got.json() == created

    patched = node_client.patch(
        f"/v1/node/agents/{created['id']}", headers=headers, json={"display_name": "Renamed", "color": None}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "Renamed" and patched.json()["color"] is None
    assert patched.json()["slug"] == "agent-1" and patched.json()["position"] == 0
    assert patched.json()["updated_at"] >= created["updated_at"]

    after = node_client.get("/v1/node/agents", headers={**headers, "If-None-Match": body["etag"]})
    assert after.status_code == 200 and after.json()["etag"] != body["etag"]

    gone = node_client.delete(f"/v1/node/agents/{created['id']}", headers=headers)
    assert gone.status_code == 204 and gone.content == b""
    assert node_client.get("/v1/node/agents", headers=headers).json()["agents"] == []
    missing = node_client.get(f"/v1/node/agents/{created['id']}", headers=headers)
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


def test_etag_depends_on_id_updated_at_and_position():
    base = [{"id": "ag_a", "updated_at": 1.0, "position": 0}]
    assert agents_etag(base) == agents_etag([dict(base[0], display_name="other")])
    assert agents_etag(base) != agents_etag([dict(base[0], updated_at=2.0)])
    assert agents_etag(base) != agents_etag([dict(base[0], position=1)])
    assert agents_etag([]) != agents_etag(base)


def test_watch_cannot_write_agents(node_client, mongo_db, nodes, signing_key):
    account, device, key = new_account(node_client, "ios")
    grant_plan(mongo_db, account)
    ios = node_headers(nodes["home"], user=user_token(signing_key, account, device, "ios", NODE_A))
    created = create(node_client, ios)
    watch = add_device(node_client, key, "watchos")
    headers = node_headers(nodes["home"], user=user_token(signing_key, account, watch, "watchos", NODE_A))
    assert node_client.get("/v1/node/agents", headers=headers).status_code == 200
    for method, path, kwargs in (
        ("post", "/v1/node/agents", {"json": agent_body(3)}),
        ("patch", f"/v1/node/agents/{created['id']}", {"json": {"display_name": "x"}}),
        ("delete", f"/v1/node/agents/{created['id']}", {}),
        ("put", "/v1/node/agents/order", {"json": {"agent": created["id"], "index": 0}}),
    ):
        r = getattr(node_client, method)(path, headers=headers, **kwargs)
        assert r.status_code == 403 and r.json()["error"] == "forbidden", (method, path)
    assert node_client.get(f"/v1/node/agents/{created['id']}", headers=ios).status_code == 200


def test_watch_reads_the_agents_of_its_account(node_client, mongo_db, nodes, signing_key):
    account, device, key = new_account(node_client, "ios")
    grant_plan(mongo_db, account)
    ios = node_headers(nodes["home"], user=user_token(signing_key, account, device, "ios", NODE_A))
    create(node_client, ios)
    watch = add_device(node_client, key, "watchos")
    headers = node_headers(nodes["home"], user=user_token(signing_key, account, watch, "watchos", NODE_A))
    listed = node_client.get("/v1/node/agents", headers=headers)
    assert listed.status_code == 200 and len(listed.json()["agents"]) == 1
    assert node_client.get("/v1/node/agents/agent-1", headers=headers).status_code == 200


def test_agent_writes_need_a_plan(node_client, mongo_db, nodes, signing_key):
    account, headers = caller(node_client, mongo_db, nodes, signing_key)
    first = create(node_client, headers)
    with db(mongo_db) as d:
        d["entitlements"].delete_one({"_id": account})
    for method, path, kwargs in (
        ("post", "/v1/node/agents", {"json": agent_body(2)}),
        ("patch", f"/v1/node/agents/{first['id']}", {"json": {"display_name": "x"}}),
        ("put", "/v1/node/agents/order", {"json": {"agent": first["id"], "index": 0}}),
    ):
        r = getattr(node_client, method)(path, headers=headers, **kwargs)
        assert r.status_code == 402 and r.json()["error"] == "plan_required", (method, path)
    assert node_client.get("/v1/node/agents", headers=headers).status_code == 200
    assert node_client.get(f"/v1/node/agents/{first['id']}", headers=headers).status_code == 200
    # Deleting works without a plan: sealed secrets can always be removed.
    assert node_client.delete(f"/v1/node/agents/{first['id']}", headers=headers).status_code == 204


def test_expired_plan_is_no_plan(node_client, mongo_db, nodes, signing_key):
    account, headers = caller(node_client, mongo_db, nodes, signing_key)
    with db(mongo_db) as d:
        d["entitlements"].update_one({"_id": account}, {"$set": {"expires_at": 1.0}})
    r = node_client.post("/v1/node/agents", headers=headers, json=agent_body())
    assert r.status_code == 402 and r.json()["error"] == "plan_required"


def test_get_agent_by_slug(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    created = create(node_client, headers, slug="my-agent")
    r = node_client.get("/v1/node/agents/my-agent", headers=headers)
    assert r.status_code == 200 and r.json()["id"] == created["id"]
    assert node_client.get("/v1/node/agents/nope", headers=headers).status_code == 404


def test_large_spec_is_accepted(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    spec = {
        "system_prompt": "é" * 20000,
        "endpoints": [{"sealed": "A" * 2048, "n": n} for n in range(4)],
        "retention_days": "forever",
    }
    r = node_client.post("/v1/node/agents", headers=headers, json=agent_body(1, spec=spec))
    assert r.status_code == 201, r.text
    assert node_client.get("/v1/node/agents/ag_000000000001", headers=headers).json()["spec"] == spec
    r = node_client.post("/v1/node/agents", headers=headers, json=agent_body(2, spec={"blob": "a" * 300 * 1024}))
    assert r.status_code == 422 and r.json()["error"] == "invalid"


@pytest.mark.parametrize(
    "over",
    [
        {"id": "agent_1"},
        {"id": "ag_00000000000G"},
        {"id": 5},
        {"slug": "Upper"},
        {"slug": "-lead"},
        {"slug": "a" * 33},
        {"display_name": ""},
        {"display_name": "x" * 65},
        {"display_name": "bad\nname"},
        {"icon": "Wave"},
        {"icon": "a" * 65},
        {"call_type": "video"},
        {"color": "black"},
        {"spec": "text"},
        {"spec": [1]},
        {"spec": None},
        {"spec": {"blob": "a" * (256 * 1024 + 1)}},
        {"spec": {"retention_days": 0}},
        {"spec": {"retention_days": -3}},
        {"spec": {"retention_days": True}},
        {"spec": {"retention_days": 36501}},
        {"spec": {"retention_days": 1.5}},
        {"spec": {"retention_days": "week"}},
        {"extra": 1},
    ],
    ids=lambda o: next(iter(o)) + ":" + str(next(iter(o.values())))[:20],
)
def test_agent_validation_is_422(node_client, mongo_db, nodes, signing_key, over):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    r = node_client.post("/v1/node/agents", headers=headers, json=agent_body(1, **over))
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "invalid"
    assert "bad\nname" not in r.text
    assert node_client.get("/v1/node/agents", headers=headers).json()["agents"] == []


@pytest.mark.parametrize("missing", ["id", "slug", "display_name", "icon", "call_type", "spec"])
def test_new_agent_requires_its_fields(node_client, mongo_db, nodes, signing_key, missing):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    body = agent_body()
    del body[missing]
    assert node_client.post("/v1/node/agents", headers=headers, json=body).status_code == 422


def test_body_must_be_a_json_object(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    assert node_client.post("/v1/node/agents", headers=headers, json=[1]).status_code == 422
    assert node_client.post("/v1/node/agents", headers=headers, content=b"{").status_code == 422


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"id": "ag_000000000002"},
        {"slug": "Upper"},
        {"call_type": "video"},
        {"color": "black"},
        {"spec": "text"},
        {"spec": {"retention_days": 0}},
        {"display_name": ""},
    ],
    ids=lambda p: str(p)[:30],
)
def test_agent_patch_validation_is_422(node_client, mongo_db, nodes, signing_key, patch):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    created = create(node_client, headers)
    r = node_client.patch(f"/v1/node/agents/{created['id']}", headers=headers, json=patch)
    assert r.status_code == 422 and r.json()["error"] == "invalid", r.text
    assert node_client.get(f"/v1/node/agents/{created['id']}", headers=headers).json() == created


def test_patch_changes_each_field(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    created = create(node_client, headers)
    patch = {
        "slug": "renamed", "display_name": "N", "icon": "mic.fill", "color": "red",
        "call_type": "one-shot", "spec": {"k": 1},
    }
    r = node_client.patch(f"/v1/node/agents/{created['id']}", headers=headers, json=patch)
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in patch} == patch
    assert node_client.get("/v1/node/agents/renamed", headers=headers).json()["id"] == created["id"]


def test_duplicate_id_or_slug_is_409(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    create(node_client, headers, 1)
    same_id = node_client.post("/v1/node/agents", headers=headers, json=agent_body(1, slug="other"))
    assert same_id.status_code == 409 and same_id.json()["error"] == "conflict"
    same_slug = node_client.post("/v1/node/agents", headers=headers, json=agent_body(2, slug="agent-1"))
    assert same_slug.status_code == 409 and same_slug.json()["error"] == "conflict"
    second = create(node_client, headers, 3)
    patched = node_client.patch(f"/v1/node/agents/{second['id']}", headers=headers, json={"slug": "agent-1"})
    assert patched.status_code == 409 and patched.json()["error"] == "conflict"
    # An id is unique across the whole database, not per account.
    _, other = caller(node_client, mongo_db, nodes, signing_key)
    taken = node_client.post("/v1/node/agents", headers=other, json=agent_body(1, slug="mine"))
    assert taken.status_code == 409 and taken.json()["error"] == "conflict"
    assert node_client.get("/v1/node/agents", headers=other).json()["agents"] == []


def test_same_slug_in_two_accounts_is_fine(node_client, mongo_db, nodes, signing_key):
    _, one = caller(node_client, mongo_db, nodes, signing_key)
    _, two = caller(node_client, mongo_db, nodes, signing_key)
    create(node_client, one, 1)
    create(node_client, two, 2, slug="agent-1")


def test_agent_limit_is_403(node_config, mongo_db, fake_verifier, signing_key):
    for client in served_node(replace(node_config, max_cloud_agents=2), mongo_db, fake_verifier, signing_key):
        secrets = register_nodes(client)

        def headers_for(account, device):
            return node_headers(secrets["home"], user=user_token(signing_key, account, device, "ios", NODE_A))

        account, device, _ = new_account(client)
        grant_plan(mongo_db, account)
        headers = headers_for(account, device)
        create(client, headers, 1)
        create(client, headers, 2)
        r = client.post("/v1/node/agents", headers=headers, json=agent_body(3))
        assert r.status_code == 403 and r.json()["error"] == "limit"
        # Deleting one makes room; the limit is per account.
        assert client.delete("/v1/node/agents/ag_000000000001", headers=headers).status_code == 204
        create(client, headers, 3)
        other, other_device, _ = new_account(client)
        grant_plan(mongo_db, other)
        create(client, headers_for(other, other_device), 4)


def ids_by_position(client, headers):
    agents = client.get("/v1/node/agents", headers=headers).json()["agents"]
    return [(a["id"], a["position"]) for a in agents]


def test_order_moves_and_renumbers(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    a, b, c = (create(node_client, headers, n)["id"] for n in (1, 2, 3))
    assert ids_by_position(node_client, headers) == [(a, 0), (b, 1), (c, 2)]
    before = node_client.get("/v1/node/agents", headers=headers).json()["etag"]

    r = node_client.put("/v1/node/agents/order", headers=headers, json={"agent": c, "index": 0})
    assert r.status_code == 200, r.text
    assert [(x["id"], x["position"]) for x in r.json()["agents"]] == [(c, 0), (a, 1), (b, 2)]
    assert r.json()["etag"] != before and r.headers["ETag"] == r.json()["etag"]
    assert ids_by_position(node_client, headers) == [(c, 0), (a, 1), (b, 2)]

    r = node_client.put("/v1/node/agents/order", headers=headers, json={"agent": c, "index": 99})
    assert [x["id"] for x in r.json()["agents"]] == [a, b, c]
    assert ids_by_position(node_client, headers) == [(a, 0), (b, 1), (c, 2)]

    r = node_client.put("/v1/node/agents/order", headers=headers, json={"agent": b, "index": -4})
    assert [x["id"] for x in r.json()["agents"]] == [b, a, c]


def test_order_renumbers_after_a_delete(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    a, b, c = (create(node_client, headers, n)["id"] for n in (1, 2, 3))
    assert node_client.delete(f"/v1/node/agents/{b}", headers=headers).status_code == 204
    d = create(node_client, headers, 4)
    r = node_client.put("/v1/node/agents/order", headers=headers, json={"agent": d["id"], "index": 1})
    assert [(x["id"], x["position"]) for x in r.json()["agents"]] == [(a, 0), (d["id"], 1), (c, 2)]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"agent": "ag_000000000001"},
        {"index": 0},
        {"agent": 1, "index": 0},
        {"agent": "ag_000000000001", "index": "0"},
        {"agent": "ag_000000000001", "index": True},
        {"agent": "ag_000000000001", "index": 0.5},
    ],
    ids=str,
)
def test_order_validation_is_422(node_client, mongo_db, nodes, signing_key, body):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    create(node_client, headers, 1)
    r = node_client.put("/v1/node/agents/order", headers=headers, json=body)
    assert r.status_code == 422 and r.json()["error"] == "invalid"


def test_order_of_unknown_agent_is_404(node_client, mongo_db, nodes, signing_key):
    _, headers = caller(node_client, mongo_db, nodes, signing_key)
    r = node_client.put("/v1/node/agents/order", headers=headers, json={"agent": "ag_000000000009", "index": 0})
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_agents_of_another_account_are_404(node_client, mongo_db, nodes, signing_key):
    _, owner = caller(node_client, mongo_db, nodes, signing_key)
    _, other = caller(node_client, mongo_db, nodes, signing_key)
    mine = create(node_client, owner, 1)["id"]
    assert node_client.get("/v1/node/agents", headers=other).json()["agents"] == []
    assert node_client.get(f"/v1/node/agents/{mine}", headers=other).status_code == 404
    assert node_client.get("/v1/node/agents/agent-1", headers=other).status_code == 404
    patched = node_client.patch(f"/v1/node/agents/{mine}", headers=other, json={"display_name": "x"})
    assert patched.status_code == 404
    assert node_client.delete(f"/v1/node/agents/{mine}", headers=other).status_code == 404
    moved = node_client.put("/v1/node/agents/order", headers=other, json={"agent": mine, "index": 0})
    assert moved.status_code == 404
    kept = node_client.get(f"/v1/node/agents/{mine}", headers=owner).json()
    assert kept["display_name"] == "Agent 1"


def test_agent_routes_need_node_credentials(node_client, nodes):
    assert node_client.get("/v1/node/agents").status_code == 401
    assert node_client.get("/v1/node/agents", headers=node_headers(nodes["home"])).status_code == 401


def test_retention_change_rewrites_call_expiry(node_client, mongo_db, nodes, signing_key):
    account, headers = caller(node_client, mongo_db, nodes, signing_key)
    agent = create(node_client, headers, 1)["id"]
    other_agent = create(node_client, headers, 2)["id"]
    t = 1_700_000_000.0
    old = t + 30 * 86400

    def call_doc(call_id, owner, agent_id):
        return {"_id": call_id, "account": owner, "agent": agent_id, "created_at": t, "expires_at": old,
                "purge_at": datetime.fromtimestamp(old, UTC), "open": False}

    def entry_doc(call_id, owner, agent_id):
        return {"_id": f"{call_id}:0", "account": owner, "call": call_id, "agent": agent_id, "seq": 0,
                "call_created_at": t, "expires_at": old, "purge_at": datetime.fromtimestamp(old, UTC)}

    with db(mongo_db) as d:
        d["cloud_calls"].insert_many([
            call_doc("c_1", account, agent), call_doc("c_2", account, other_agent), call_doc("c_3", "acc_other", agent),
        ])
        d["cloud_call_entries"].insert_many([
            entry_doc("c_1", account, agent),
            entry_doc("c_2", account, other_agent),
            entry_doc("c_3", "acc_other", agent),
        ])

    r = node_client.patch(
        f"/v1/node/agents/{agent}", headers=headers, json={"spec": {"stt": {"sealed": "BBBB"}, "retention_days": 7}}
    )
    assert r.status_code == 200, r.text
    new = t + 7 * 86400
    with db(mongo_db) as d:
        for coll, doc_id in (("cloud_calls", "c_1"), ("cloud_call_entries", "c_1:0")):
            doc = d[coll].find_one({"_id": doc_id})
            assert doc["expires_at"] == new, coll
            assert doc["purge_at"] == datetime.fromtimestamp(new, UTC), coll
        for coll, doc_id in (
            ("cloud_calls", "c_2"),
            ("cloud_call_entries", "c_2:0"),
            ("cloud_calls", "c_3"),
            ("cloud_call_entries", "c_3:0"),
        ):
            assert d[coll].find_one({"_id": doc_id})["expires_at"] == old, (coll, doc_id)

    # "forever" and a missing value mean the ceiling and the default; a value above the ceiling is capped.
    for retention, days in (("forever", 365), (None, 90), (9999, 365)):
        spec = {"stt": {"sealed": "BBBB"}, "retention_days": retention}
        assert node_client.patch(f"/v1/node/agents/{agent}", headers=headers, json={"spec": spec}).status_code == 200
        with db(mongo_db) as d:
            assert d["cloud_calls"].find_one({"_id": "c_1"})["expires_at"] == t + days * 86400


def test_patch_without_retention_change_leaves_expiry(node_client, mongo_db, nodes, signing_key):
    account, headers = caller(node_client, mongo_db, nodes, signing_key)
    agent = create(node_client, headers, 1)["id"]
    with db(mongo_db) as d:
        d["cloud_calls"].insert_one(
            {"_id": "c_1", "account": account, "agent": agent, "created_at": 5.0, "expires_at": 123.0}
        )
    for patch in ({"display_name": "x"}, {"spec": {"other": 1, "retention_days": 30}}):
        assert node_client.patch(f"/v1/node/agents/{agent}", headers=headers, json=patch).status_code == 200
    with db(mongo_db) as d:
        assert d["cloud_calls"].find_one({"_id": "c_1"})["expires_at"] == 123.0
