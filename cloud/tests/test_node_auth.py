"""The credential of a node, the user token it forwards, the call grant and `GET /v1/node/me`."""

import time
from dataclasses import replace
from types import SimpleNamespace

import jwt
import pytest

from node_fixtures import (
    NODE_A,
    NODE_B,
    PUBLIC_URL,
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
from test_sessions_api import new_signing_key
from wristcall_cloud.api_common import ApiError
from wristcall_cloud.node_auth import (
    EXPIRED_GRANT_WINDOW_S,
    GRANT_SLACK_S,
    GRANT_TYPE,
    NodeCaller,
    issue_grant,
    require_manager,
    verify_grant,
)
from wristcall_cloud.session_tokens import AccountTokens
from wristcall_cloud.signing import server_token_claims


CALL = "c_0123456789abcdef"


def setup_me(client, mongo_db, nodes, signing_key, platform="ios"):
    account, device, key = new_account(client, platform)
    token = user_token(signing_key, account, device, platform, NODE_A)
    return account, device, key, node_headers(nodes["home"], user=token)


def test_node_route_without_credential_is_401(node_client, nodes, node_app, signing_key):
    account, device, _ = new_account(node_client)
    user = user_token(signing_key, account, device, "ios", NODE_A)
    for headers in (
        {"X-Wristcall-User-Token": user},
        {**node_headers("wc_node_" + "x" * 43), "X-Wristcall-User-Token": user},
        {**node_headers("not-a-node-secret"), "X-Wristcall-User-Token": user},
        {"Authorization": nodes["home"], "X-Wristcall-User-Token": user},
    ):
        r = node_client.get("/v1/node/me", headers=headers)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized", headers
    # A node that was disabled stops at once.
    node_client.portal.call(node_app.state.cloud.nodes.update_one, {"_id": "home"}, {"$set": {"enabled": False}})
    r = node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=user))
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_me_without_user_token_is_401(node_client, nodes):
    r = node_client.get("/v1/node/me", headers=node_headers(nodes["home"]))
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_me_returns_account_device_plan_usage(node_client, mongo_db, nodes, signing_key):
    account, device, _, headers = setup_me(node_client, mongo_db, nodes, signing_key)
    grant_plan(mongo_db, account, "full")
    r = node_client.get("/v1/node/me", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account"] == account
    assert body["plan"]["plan"] == "full"
    assert body["device"] == {"id": device, "platform": "ios"}
    assert body["agents"] == []
    assert body["usage"]["quota_s"] == 36000 and body["usage"]["used_s"] == 0
    assert body["usage"]["remaining_s"] == 36000 and len(body["usage"]["month"]) == 7


def test_me_without_plan_and_with_agents_and_usage(node_client, mongo_db, nodes, signing_key):
    account, device, _, headers = setup_me(node_client, mongo_db, nodes, signing_key)
    with db(mongo_db) as d:
        now = time.time()
        for agent_id, position, created in (("ag_b", 0, now), ("ag_a", 0, now - 10), ("ag_c", 1, now - 20)):
            d["cloud_agents"].insert_one(
                {"_id": agent_id, "account": account, "slug": agent_id, "display_name": agent_id, "icon": None,
                 "color": None, "call_type": "conversation", "position": position, "spec": {"k": 1},
                 "created_at": created, "updated_at": created, "secret_field": "never shown"}
            )
        d["cloud_agents"].insert_one({"_id": "ag_x", "account": "acc_other", "position": 0, "created_at": 0})
        month = time.strftime("%Y-%m", time.gmtime())
        d["usage"].insert_one({"_id": f"{account}:{month}", "account": account, "month": month, "total_s": 40000})
    body = node_client.get("/v1/node/me", headers=headers).json()
    assert body["plan"] is None
    assert [a["id"] for a in body["agents"]] == ["ag_a", "ag_b", "ag_c"]
    assert set(body["agents"][0]) == {
        "id", "slug", "display_name", "icon", "color", "call_type", "position", "spec", "created_at", "updated_at"
    }
    assert body["usage"]["used_s"] == 40000 and body["usage"]["remaining_s"] == 0


def test_user_token_for_another_node_is_401(node_client, mongo_db, nodes, signing_key):
    account, device, _ = new_account(node_client)
    away = user_token(signing_key, account, device, "ios", NODE_B)
    r = node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=away))
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_user_token_without_did_or_plt_is_401(node_client, nodes, signing_key):
    account, device, _ = new_account(node_client)
    plain = signing_key.sign(
        server_token_claims(
            issuer=PUBLIC_URL, account=account, client_id="client-ios", audience=NODE_A, now=time.time()
        )
    )
    only_did = signing_key.sign(
        server_token_claims(
            issuer=PUBLIC_URL, account=account, client_id=None, audience=NODE_A, now=time.time(), device=device
        )
    )
    for token in (plain, only_did):
        r = node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=token))
        assert r.status_code == 401, r.text


def test_user_token_checks(node_client, nodes, signing_key):
    account, device, _ = new_account(node_client)
    now = time.time()
    good = user_token(signing_key, account, device, "ios", NODE_A)
    assert node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=good)).status_code == 200
    # Expired beyond the leeway, issued in the future beyond it, within it, platform not known, garbage.
    expired = user_token(signing_key, account, device, "ios", NODE_A, iat=now - 400, ttl_s=300)
    barely = user_token(signing_key, account, device, "ios", NODE_A, iat=now - 330, ttl_s=300)
    future = user_token(signing_key, account, device, "ios", NODE_A, iat=now + 120)
    soon = user_token(signing_key, account, device, "ios", NODE_A, iat=now + 30)
    unknown_platform = user_token(signing_key, account, device, "tv", NODE_A)
    wrong_issuer = signing_key.sign(
        {**server_token_claims(issuer="https://other.test", account=account, client_id=None, audience=NODE_A, now=now),
         "did": device, "plt": "ios"}
    )
    other_key = user_token(new_signing_key(), account, device, "ios", NODE_A)
    wrong_typ = signing_key.sign(
        {**server_token_claims(issuer=PUBLIC_URL, account=account, client_id=None, audience=NODE_A, now=now),
         "did": device, "plt": "ios"},
        typ="JWT",
    )
    for token, status in (
        (expired, 401), (barely, 200), (future, 401), (soon, 200), (unknown_platform, 401), (wrong_issuer, 401),
        (other_key, 401), (wrong_typ, 401), ("not.a.jwt", 401), ("x" * 5000, 401),
    ):
        r = node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=token))
        assert r.status_code == status, (status, r.text)


def test_account_token_or_grant_as_user_token_is_401(node_client, node_app, nodes, signing_key):
    account, device, _ = new_account(node_client)
    tokens = AccountTokens(signing_key, PUBLIC_URL)
    account_token, _ = tokens.issue(account, device, "ios")
    caller = NodeCaller(account=account, device=device, platform="ios", node={"_id": "home", "url": NODE_A})
    grant = issue_grant(signing_key, issuer=PUBLIC_URL, caller=caller, call_id=CALL, max_s=60, now=time.time())
    for token in (account_token, grant):
        r = node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=token))
        assert r.status_code == 401, r.text


def test_revoked_device_is_403(node_client, mongo_db, nodes, signing_key):
    account, device, _, headers = setup_me(node_client, mongo_db, nodes, signing_key)
    assert node_client.get("/v1/node/me", headers=headers).status_code == 200
    with db(mongo_db) as d:
        assert d["devices"].delete_one({"_id": device}).deleted_count == 1
    r = node_client.get("/v1/node/me", headers=headers)
    assert r.status_code == 403 and r.json()["error"] == "device_revoked"


def test_device_of_another_account_is_403(node_client, nodes, signing_key):
    account, _, _ = new_account(node_client)
    _, other_device, _ = new_account(node_client)
    token = user_token(signing_key, account, other_device, "ios", NODE_A)
    r = node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=token))
    assert r.status_code == 403 and r.json()["error"] == "device_revoked"


def test_node_rate_limit_is_429(node_config, mongo_db, fake_verifier, signing_key):
    for c in served_node(replace(node_config, node_requests_per_minute=2), mongo_db, fake_verifier, signing_key):
        secrets = register_nodes(c)
        account, device, _ = new_account(c)
        home = node_headers(secrets["home"], user=user_token(signing_key, account, device, "ios", NODE_A))
        away = node_headers(secrets["away"], user=user_token(signing_key, account, device, "ios", NODE_B))
        assert c.get("/v1/node/me", headers=home).status_code == 200
        assert c.get("/v1/node/me", headers=home).status_code == 200
        r = c.get("/v1/node/me", headers=home)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1
        # The limit is per node.
        assert c.get("/v1/node/me", headers=away).status_code == 200


def test_manager_platforms(node_client):
    node = {"_id": "home"}
    for platform in ("ios", "ipados", "macos", "web"):
        require_manager(NodeCaller(account="a", device="d", platform=platform, node=node))
    with pytest.raises(ApiError) as e:
        require_manager(NodeCaller(account="a", device="d", platform="watchos", node=node))
    assert e.value.status == 403 and e.value.code == "forbidden"


def test_watch_token_is_accepted_for_me(node_client, mongo_db, nodes, signing_key):
    account, _, key = new_account(node_client)
    watch = add_device(node_client, key, "watchos")
    token = user_token(signing_key, account, watch, "watchos", NODE_A)
    body = node_client.get("/v1/node/me", headers=node_headers(nodes["home"], user=token)).json()
    assert body["device"] == {"id": watch, "platform": "watchos"}



def grant_for(signing_key, *, now, max_s=60, node_id="home", call_id=CALL):
    caller = NodeCaller(account="acc_x", device="dev_x", platform="ios", node={"_id": node_id, "url": NODE_A})
    return issue_grant(signing_key, issuer=PUBLIC_URL, caller=caller, call_id=call_id, max_s=max_s, now=now)


def test_grant_roundtrip_and_expiry(node_app, signing_key):
    request = SimpleNamespace(app=node_app)
    node = {"_id": "home", "url": NODE_A}
    now = time.time()
    token = grant_for(signing_key, now=now, max_s=60)
    got = verify_grant(request, node, CALL, token, allow_expired=False)
    assert (got.account, got.device, got.platform, got.call_id, got.node_id, got.expired) == (
        "acc_x", "dev_x", "ios", CALL, "home", False
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 60 + GRANT_SLACK_S and claims["aud"] == PUBLIC_URL
    assert jwt.get_unverified_header(token)["typ"] == GRANT_TYPE

    over = grant_for(signing_key, now=now - (60 + GRANT_SLACK_S + 10))
    with pytest.raises(ApiError) as e:
        verify_grant(request, node, CALL, over, allow_expired=False)
    assert e.value.status == 401
    assert verify_grant(request, node, CALL, over, allow_expired=True).expired is True

    ancient = grant_for(signing_key, now=now - (60 + GRANT_SLACK_S + EXPIRED_GRANT_WINDOW_S + 10))
    for allow in (False, True):
        with pytest.raises(ApiError) as e:
            verify_grant(request, node, CALL, ancient, allow_expired=allow)
        assert e.value.status == 401


def test_grant_is_bound_to_node_and_call(node_app, signing_key):
    request = SimpleNamespace(app=node_app)
    token = grant_for(signing_key, now=time.time())
    cases = (
        ({"_id": "away", "url": NODE_B}, CALL, token),
        ({"_id": "home", "url": NODE_A}, "c_ffffffffffffffff", token),
        ({"_id": "home", "url": NODE_A}, CALL, None),
        ({"_id": "home", "url": NODE_A}, CALL, ""),
        ({"_id": "home", "url": NODE_A}, CALL, "garbage"),
        ({"_id": "home", "url": NODE_A}, CALL, token[:-3] + "AAA"),
    )
    for node, call_id, value in cases:
        with pytest.raises(ApiError) as e:
            verify_grant(request, node, call_id, value, allow_expired=True)
        assert e.value.status == 401 and e.value.code == "unauthorized"


def test_grant_of_another_type_or_key_is_refused(node_app, signing_key):
    request = SimpleNamespace(app=node_app)
    node = {"_id": "home", "url": NODE_A}
    now = time.time()
    good = grant_for(signing_key, now=now)
    claims = jwt.decode(good, options={"verify_signature": False})
    refused = [
        signing_key.sign(claims),  # typ wc-server+jwt
        signing_key.sign(claims, typ="wc-account+jwt"),
        signing_key.sign({**claims, "iss": "https://other.test"}, typ=GRANT_TYPE),
        signing_key.sign({**claims, "aud": NODE_A}, typ=GRANT_TYPE),
        signing_key.sign({k: v for k, v in claims.items() if k != "call"}, typ=GRANT_TYPE),
        signing_key.sign({**claims, "plt": "tv"}, typ=GRANT_TYPE),
        grant_for(new_signing_key(), now=now),
    ]
    for token in refused:
        with pytest.raises(ApiError) as e:
            verify_grant(request, node, CALL, token, allow_expired=True)
        assert e.value.status == 401


def test_nodes_routes_are_not_shadowed(node_client):
    assert node_client.get("/v1/node/me").status_code == 401
