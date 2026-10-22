"""`POST /v1/route`: the Cloud picks the node of a call (or of management) and hands the app that node's URL with a
per-server token bound to it, plus a fallback node. Heartbeats are written straight to Mongo (always fresh here)."""

import logging
import secrets
import time
from dataclasses import replace

import jwt
import pytest

from node_fixtures import (  # noqa: F401
    NODE_A,
    NODE_B,
    PUBLIC_URL,
    db,
    grant_plan,
    node_app,
    node_client,
    node_config,
    node_headers,
    nodes,
    register_nodes,
    served_node,
    signing_key,
)
from test_sessions_api import bearer, create_account
from wristcall_cloud.limits import AccountCreationLimiter
from wristcall_cloud.signing import ALGORITHM, TOKEN_TYPE

BOTH = ["operator_stt", "operator_tts"]


def account(c, mongo_db, plan: str | None = None, platform: str = "ios") -> tuple[str, str, dict[str, str]]:
    """A new account (on `plan` when given): (account id, device id, headers with its access token)."""
    created = create_account(c, platform=platform)
    if plan is not None:
        grant_plan(mongo_db, created["account"], plan)
    return created["account"], created["device"], bearer(created["access_token"])


def add_agent(mongo_db, owner: str, call_type: str = "conversation") -> str:
    agent_id = f"ag_{secrets.token_hex(6)}"
    with db(mongo_db) as d:
        d["cloud_agents"].insert_one(
            {
                "_id": agent_id,
                "account": owner,
                "slug": agent_id.replace("_", "-"),
                "display_name": "Agent",
                "icon": "phone",
                "call_type": call_type,
                "spec": {},
                "position": 0,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        )
    return agent_id


def beat(d, node_id: str, **fields) -> None:
    d["nodes"].update_one({"_id": node_id}, {"$set": {"last_seen_at": time.time(), **fields}})


def set_caps(d, node_id: str, capabilities: list[str]) -> None:
    d["nodes"].update_one({"_id": node_id}, {"$set": {"capabilities": capabilities}})


def route(c, headers: dict[str, str], **body):
    return c.post("/v1/route", json=body, headers=headers)


def claims_of(token: str, key, audience: str) -> dict:
    return jwt.decode(token, key.public_key, algorithms=[ALGORITHM], audience=audience, issuer=PUBLIC_URL)


def all_healthy(mongo_db, *, caps=BOTH, **home) -> None:
    with db(mongo_db) as d:
        for node_id in ("home", "away"):
            set_caps(d, node_id, caps)
            beat(d, node_id, **(home if node_id == "home" else {}))


def test_route_without_nodes_is_503(node_client, mongo_db):
    owner, _, headers = account(node_client, mongo_db, "full")
    route(node_client, headers, purpose="manage")  # provisions "Voice notes"
    with db(mongo_db) as d:
        voice_notes = d["cloud_agents"].find_one({"account": owner, "slug": "voice-notes"})
    r = route(node_client, headers, purpose="call", agent=voice_notes["_id"])
    assert r.status_code == 503
    assert r.json()["error"] == "no_node_available"
    assert r.headers["Retry-After"] == "30"


def test_route_needs_a_plan_to_call(node_client, nodes, mongo_db):
    all_healthy(mongo_db)
    owner, _, headers = account(node_client, mongo_db)
    agent = add_agent(mongo_db, owner)
    for body in ({"purpose": "call", "agent": agent}, {"purpose": "manage"}):
        r = route(node_client, headers, **body)
        assert r.status_code == 402, body
        assert r.json()["error"] == "plan_required"
    with db(mongo_db) as d:
        assert d["servers"].find_one({"account": owner}) is None


def test_route_provisions_on_first_use(node_client, nodes, mongo_db):
    all_healthy(mongo_db)
    owner, _, headers = account(node_client, mongo_db, "custom")
    agent = add_agent(mongo_db, owner)
    with db(mongo_db) as d:
        assert d["servers"].find_one({"account": owner}) is None
    r = route(node_client, headers, purpose="call", agent=agent)
    assert r.status_code == 200, r.text
    with db(mongo_db) as d:
        entry = d["servers"].find_one({"account": owner, "kind": "cloud", "managed": True})
    assert entry is not None and entry["url"] == PUBLIC_URL


def test_route_picks_node_and_fallback(node_client, nodes, mongo_db, signing_key, caplog):
    all_healthy(mongo_db, priority=10)
    owner, _, headers = account(node_client, mongo_db, "full")
    agent = add_agent(mongo_db, owner)
    with caplog.at_level(logging.INFO, logger="wristcall_cloud"):
        r = route(node_client, headers, purpose="call", agent=agent)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["node"] == {"id": "home", "url": NODE_A, "region": "gru"}
    claims = claims_of(body["token"], signing_key, NODE_A)
    assert body["expires_at"] == claims["exp"]
    fallback = body["fallback"]
    assert fallback["node"] == {"id": "away", "url": NODE_B, "region": "gru"}
    fallback_claims = claims_of(fallback["token"], signing_key, NODE_B)
    assert fallback_claims["aud"] == NODE_B
    assert fallback["expires_at"] == fallback_claims["exp"]
    assert "route issued (node home, purpose call)" in caplog.text
    for secret in (NODE_A, NODE_B, body["token"], fallback["token"]):
        assert secret not in caplog.text

    r = route(node_client, headers, purpose="call", agent=agent, avoid=["home"])
    assert r.status_code == 200, r.text
    assert r.json()["node"]["id"] == "away"
    assert r.json()["fallback"] is None


def test_route_token_works_on_its_node_only(node_client, nodes, mongo_db, signing_key):
    all_healthy(mongo_db, priority=10)
    owner, device, headers = account(node_client, mongo_db, "custom", platform="ios")
    with db(mongo_db) as d:
        d["devices"].update_one({"_id": device}, {"$set": {"platform": "ipados"}})
    r = route(node_client, headers, purpose="manage")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert r.json()["node"]["url"] == NODE_A
    assert jwt.get_unverified_header(token)["typ"] == TOKEN_TYPE == "wc-server+jwt"
    claims = claims_of(token, signing_key, NODE_A)
    assert claims["sub"] == owner
    assert claims["did"] == device
    assert claims["plt"] == "ipados"
    assert claims["iss"] == PUBLIC_URL
    assert claims["exp"] - claims["iat"] == 300
    assert node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=token)).status_code == 200
    assert node_client.get("/v1/node/me", headers=node_headers(nodes["away"], user=token)).status_code == 401
    r = node_client.post("/v1/server-tokens", json={"audience": NODE_A}, headers=headers)
    assert r.status_code == 422


def test_route_respects_capacity_by_call_type(node_client, nodes, mongo_db):
    with db(mongo_db) as d:
        set_caps(d, "home", BOTH)
        beat(
            d,
            "home",
            active_by_type={"conversation": 3, "one_way": 0},
            capacity={"conversation": 3, "one_way": 10},
        )
        d["nodes"].update_one({"_id": "away"}, {"$set": {"enabled": False}})
    owner, _, headers = account(node_client, mongo_db, "full")
    conversation = add_agent(mongo_db, owner, "conversation")
    one_shot = add_agent(mongo_db, owner, "one-shot")
    r = route(node_client, headers, purpose="call", agent=conversation)
    assert r.status_code == 503
    assert r.json()["error"] == "no_node_available"
    r = route(node_client, headers, purpose="call", agent=one_shot)
    assert r.status_code == 200, r.text
    assert r.json()["node"]["id"] == "home"
    r = route(node_client, headers, purpose="manage")
    assert r.status_code == 200, r.text
    assert r.json()["node"]["id"] == "home"


def test_full_call_skips_nodes_without_operator_voices(node_client, nodes, mongo_db):
    all_healthy(mongo_db, caps=["operator_stt"])
    owner, _, headers = account(node_client, mongo_db, "full")
    r = route(node_client, headers, purpose="call", agent=add_agent(mongo_db, owner))
    assert r.status_code == 503
    owner, _, headers = account(node_client, mongo_db, "custom")
    r = route(node_client, headers, purpose="call", agent=add_agent(mongo_db, owner))
    assert r.status_code == 200, r.text


def test_full_call_skips_nodes_whose_providers_fail(node_client, nodes, mongo_db):
    all_healthy(mongo_db, providers_ok=False, priority=10)
    owner, _, headers = account(node_client, mongo_db, "full")
    r = route(node_client, headers, purpose="call", agent=add_agent(mongo_db, owner))
    assert r.status_code == 200, r.text
    assert r.json()["node"]["id"] == "away"
    assert r.json()["fallback"] is None


def test_lapsed_plan_manages_but_does_not_call(node_client, nodes, mongo_db):
    all_healthy(mongo_db)
    owner, _, headers = account(node_client, mongo_db, "custom")
    agent = add_agent(mongo_db, owner)
    assert route(node_client, headers, purpose="manage").status_code == 200  # provisions the Cloud entry
    with db(mongo_db) as d:
        d["entitlements"].delete_one({"_id": owner})
    r = route(node_client, headers, purpose="call", agent=agent)
    assert r.status_code == 402
    assert r.json()["error"] == "plan_required"
    r = route(node_client, headers, purpose="manage")
    assert r.status_code == 200, r.text
    assert r.json()["node"]["id"] in ("home", "away")


def test_route_body_errors(node_client, nodes, mongo_db):
    all_healthy(mongo_db)
    owner, _, headers = account(node_client, mongo_db, "custom")
    agent = add_agent(mongo_db, owner)
    bad = [
        {},
        {"purpose": "x"},
        {"purpose": "call"},
        {"purpose": "call", "agent": 5},
        {"purpose": "manage", "agent": agent},
        {"purpose": "manage", "avoid": [f"n{i}" for i in range(9)]},
        {"purpose": "manage", "avoid": ["Home!"]},
        {"purpose": "manage", "avoid": "home"},
        {"purpose": "manage", "extra": 1},
    ]
    for body in bad:
        r = node_client.post("/v1/route", json=body, headers=headers)
        assert r.status_code == 422, body
        assert r.json()["error"] == "invalid", body
    other, _, _ = account(node_client, mongo_db, "custom")
    for missing in (add_agent(mongo_db, other), "ag_000000000000"):
        r = route(node_client, headers, purpose="call", agent=missing)
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"
    r = route(node_client, headers, purpose="manage", avoid=[f"n{i}" for i in range(8)])
    assert r.status_code == 200, r.text


def test_route_auth(node_client, nodes, mongo_db, fake_verifier):
    all_healthy(mongo_db)
    assert node_client.post("/v1/route", json={"purpose": "manage"}).status_code == 401
    fake_verifier.add("zitadel-token", "user-1", client_id="client-ios")
    r = route(node_client, {"Authorization": "Bearer zitadel-token"}, purpose="manage")
    assert r.status_code == 401
    _, device, headers = account(node_client, mongo_db, "custom")
    with db(mongo_db) as d:
        d["devices"].delete_one({"_id": device})
    r = route(node_client, headers, purpose="manage")
    assert r.status_code == 401


def test_route_device_gone_after_authentication_is_403(node_client, nodes, mongo_db, monkeypatch):
    all_healthy(mongo_db)
    _, _, headers = account(node_client, mongo_db, "custom")
    accounts = node_client.app.state.accounts
    real = accounts.device
    seen = []

    async def vanishing(owner, device_id):
        seen.append(device_id)
        return await real(owner, device_id) if len(seen) == 1 else None

    monkeypatch.setattr(accounts, "device", vanishing)
    r = route(node_client, headers, purpose="manage")
    assert r.status_code == 403
    assert r.json()["error"] == "device_revoked"


def test_route_without_signing_key_is_404(client):
    r = client.post("/v1/route", json={"purpose": "manage"})
    assert r.status_code == 404
    assert r.json()["error"] == "not_configured"


def test_avoid_reports_are_per_account(node_client, nodes, mongo_db):
    with db(mongo_db) as d:
        d["nodes"].update_one({"_id": "away"}, {"$set": {"kind": "on_demand"}})
        d["nodes"].update_one({"_id": "home"}, {"$set": {"enabled": False}})
    node_client.app.state.account_limiter = AccountCreationLimiter(100, 100)  # five accounts from one address
    fourth = account(node_client, mongo_db, "custom")[2]
    r = route(node_client, fourth, purpose="manage")
    assert r.status_code == 200, r.text
    assert r.json()["node"]["id"] == "away"
    for _ in range(3):
        headers = account(node_client, mongo_db, "custom")[2]
        assert route(node_client, headers, purpose="manage", avoid=["away"]).status_code == 503
        # the same account again counts once
        assert route(node_client, headers, purpose="manage", avoid=["away"]).status_code == 503
    r = route(node_client, fourth, purpose="manage")
    assert r.status_code == 503
    assert r.json()["error"] == "no_node_available"
    assert route(node_client, fourth, purpose="manage", avoid=["ghost"]).status_code == 503
    assert "ghost" not in node_client.app.state.node_reports._seen


def test_avoid_reports_need_three_accounts(node_client, nodes, mongo_db):
    with db(mongo_db) as d:
        d["nodes"].update_one({"_id": "away"}, {"$set": {"kind": "on_demand"}})
        d["nodes"].update_one({"_id": "home"}, {"$set": {"enabled": False}})
    headers = account(node_client, mongo_db, "custom")[2]
    for _ in range(3):
        route(node_client, headers, purpose="manage", avoid=["away"])
    assert route(node_client, headers, purpose="manage").json()["node"]["id"] == "away"


@pytest.mark.parametrize(("country", "first"), [("BR", "away"), ("US", "home"), (None, "home")])
def test_route_orders_by_the_client_country(node_config, mongo_db, fake_verifier, signing_key, country, first):  # noqa: F811
    config = replace(node_config, client_country_header="CF-IPCountry")
    for c in served_node(config, mongo_db, fake_verifier, signing_key):
        register_nodes(c)
        with db(mongo_db) as d:
            beat(d, "home", region="iad")
            beat(d, "away", region="sao")
        _, _, headers = account(c, mongo_db, "custom")
        if country is not None:
            headers = {**headers, "CF-IPCountry": country}
        r = route(c, headers, purpose="manage")
        assert r.status_code == 200, r.text
        assert r.json()["node"]["id"] == first
