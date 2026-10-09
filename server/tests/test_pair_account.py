"""Pairing a device with a central account login: POST /v1/pair/account and the /v1/pairing-requests API."""

import logging

import httpx
import pytest
import respx

from oidc_fixtures import AUDIENCE, ISSUER, FakeIssuer
from test_api import api_token, device_token, h, make_client, run
from conftest import fake_config
from wristcall.config import CentralAccountConfig


def account_config(device_credential: str, **server):
    cfg = fake_config()
    return cfg.model_copy(update={
        "central_account": CentralAccountConfig(issuer=ISSUER, clients=[AUDIENCE], device_credential=device_credential),
        "server": cfg.server.model_copy(update=server),
    })


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def issuer(router):
    return FakeIssuer(router)


@pytest.fixture
def att_client(router):
    with make_client(account_config("attestation")) as c:
        yield c


@pytest.fixture
def appr_client(router):
    # The server also takes anonymous requests (pairing_approval: manual), to check the two never mix.
    with make_client(account_config("approval", pairing_approval="manual", client_ip_header="X-Real-IP")) as c:
        yield c


def link(client, token, pat):
    r = client.post("/v1/account/link", json={"token": token}, headers=h(pat))
    assert r.status_code == 200, r.text


def pair_account(client, token, device_name="Watch"):
    return client.post("/v1/pair/account", json={"token": token, "device_name": device_name})


def poll(client, poll_token):
    return client.post("/v1/pair/poll", json={"poll_token": poll_token})


def pending_request(client, issuer, pat, device_name="Watch"):
    link(client, issuer.token(), pat)
    r = pair_account(client, issuer.token(), device_name)
    assert r.status_code == 202, r.text
    return r.json()


def approve(client, request_id, token):
    return client.post(f"/v1/pairing-requests/{request_id}/approve", headers=h(token))


def test_attestation_pairs_linked_account(att_client, issuer):
    pat = api_token(att_client)
    link(att_client, issuer.token(), pat)
    r = pair_account(att_client, issuer.token(), "My Watch")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"device_id", "token"}
    me = att_client.get("/v1/me", headers=h(body["token"]))
    assert me.status_code == 200
    assert me.json()["device_name"] == "My Watch" and me.json()["user"]["handle"] == "owner"


def test_approval_waits_for_the_user(appr_client, issuer):
    pat = api_token(appr_client)
    body = pending_request(appr_client, issuer, pat, "Wrist")
    assert set(body) == {"request_id", "poll_token", "expires_at"}
    r = poll(appr_client, body["poll_token"])
    assert r.status_code == 202 and r.json() == {"request_id": body["request_id"], "expires_at": body["expires_at"]}
    listed = appr_client.get("/v1/pairing-requests", headers=h(pat))
    assert listed.status_code == 200
    assert listed.json() == {
        "requests": [{"request_id": body["request_id"], "device_name": "Wrist", "expires_at": body["expires_at"]}]
    }
    r = approve(appr_client, body["request_id"], pat)
    assert r.status_code == 200 and r.json() == {"device_name": "Wrist"}
    r = poll(appr_client, body["poll_token"])
    assert r.status_code == 200 and set(r.json()) == {"device_id", "token"}
    assert appr_client.get("/v1/me", headers=h(r.json()["token"])).json()["user"]["handle"] == "owner"
    assert appr_client.get("/v1/pairing-requests", headers=h(pat)).json() == {"requests": []}
    assert poll(appr_client, body["poll_token"]).status_code == 410


def test_account_token_cannot_approve(appr_client, issuer):
    pat = api_token(appr_client)
    body = pending_request(appr_client, issuer, pat)
    token = issuer.token()
    assert approve(appr_client, body["request_id"], token).status_code == 401
    assert appr_client.get("/v1/pairing-requests", headers=h(token)).status_code == 401
    assert appr_client.post(f"/v1/pairing-requests/{body['request_id']}/deny", headers=h(token)).status_code == 401
    assert approve(appr_client, body["request_id"], "").status_code == 401
    assert poll(appr_client, body["poll_token"]).status_code == 202


def test_api_cannot_approve_untargeted_requests(appr_client, issuer):
    pat = api_token(appr_client)
    anonymous = appr_client.post("/v1/pair", json={"device_name": "Anon"})
    assert anonymous.status_code == 202
    request_id = anonymous.json()["request_id"]
    assert appr_client.get("/v1/pairing-requests", headers=h(pat)).json() == {"requests": []}
    r = approve(appr_client, request_id, pat)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = appr_client.post(f"/v1/pairing-requests/{request_id}/deny", headers=h(pat))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert poll(appr_client, anonymous.json()["poll_token"]).status_code == 202


def test_targeted_requests_ignore_the_global_cap(appr_client, issuer):
    pat = api_token(appr_client)
    link(appr_client, issuer.token(), pat)
    # 20 anonymous requests from as many addresses (one IP may only try 10 times a minute) fill the global queue.
    for i in range(20):
        r = appr_client.post("/v1/pair", json={"device_name": "Anon"}, headers={"X-Real-IP": f"10.0.0.{i}"})
        assert r.status_code == 202
    r = appr_client.post("/v1/pair", json={"device_name": "Anon"}, headers={"X-Real-IP": "10.0.1.1"})
    assert r.status_code == 401
    r = pair_account(appr_client, issuer.token())
    assert r.status_code == 202, r.text
    assert len(appr_client.get("/v1/pairing-requests", headers=h(pat)).json()["requests"]) == 1


def test_targeted_cap_is_429(appr_client, issuer):
    pat = api_token(appr_client)
    link(appr_client, issuer.token(), pat)
    for _ in range(5):
        assert pair_account(appr_client, issuer.token()).status_code == 202
    r = pair_account(appr_client, issuer.token())
    assert r.status_code == 429 and r.json()["error"] == "too_many_requests"


def test_deny_via_api(appr_client, issuer):
    pat = api_token(appr_client)
    body = pending_request(appr_client, issuer, pat)
    r = appr_client.post(f"/v1/pairing-requests/{body['request_id']}/deny", headers=h(pat))
    assert r.status_code == 204
    r = poll(appr_client, body["poll_token"])
    assert r.status_code == 410 and r.json()["error"] == "gone"
    assert approve(appr_client, body["request_id"], pat).status_code == 404
    assert appr_client.post(f"/v1/pairing-requests/{body['request_id']}/deny", headers=h(pat)).status_code == 404


def test_pair_account_not_linked(att_client, issuer):
    api_token(att_client)
    r = pair_account(att_client, issuer.token())
    assert r.status_code == 403
    assert r.json() == {"error": "not_linked", "message": "link this account to a user on the server first"}
    assert run(att_client.app.state.pairing.list_devices()) == []


def test_pair_account_rejects_foreign_audience(att_client, issuer):
    link(att_client, issuer.token(), api_token(att_client))
    r = att_client.post("/v1/pair/account", json={"token": issuer.token(aud=["cantina-portal"]), "device_name": "w"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_account_token"
    assert run(att_client.app.state.pairing.list_devices()) == []


def test_pair_account_rejects_id_tokens_and_expired_tokens(att_client, issuer):
    link(att_client, issuer.token(), api_token(att_client))
    for token in (issuer.token(nonce="n-1"), issuer.token(exp=1), "x.y.z"):
        r = pair_account(att_client, token)
        assert r.status_code == 401 and r.json()["error"] == "invalid_account_token"


def test_pair_account_issuer_down_is_503(att_client, issuer):
    issuer.discovery.mock(side_effect=lambda request: httpx.Response(503))
    r = pair_account(att_client, issuer.token())
    assert r.status_code == 503 and r.json()["error"] == "account_unavailable"


def test_pair_account_not_configured():
    with make_client() as c:
        r = c.post("/v1/pair/account", json={"token": "x.y.z"})
        assert r.status_code == 404 and r.json()["error"] == "not_configured"
        pat = api_token(c)
        assert c.get("/v1/pairing-requests", headers=h(pat)).status_code == 404
        assert c.post("/v1/pairing-requests/1234/approve", headers=h(pat)).json()["error"] == "not_configured"
        assert c.post("/v1/pairing-requests/1234/deny", headers=h(pat)).json()["error"] == "not_configured"


def test_pair_account_bad_body(att_client, issuer):
    for body in ({}, {"token": 1}, {"token": "x.y.z", "device_name": "w" * 65}, {"token": "x.y.z", "device_name": 3}):
        assert att_client.post("/v1/pair/account", json=body).status_code == 422
    token = issuer.token()
    for name in ("w" * 65, 3):
        r = att_client.post("/v1/pair/account", json={"token": token, "device_name": name})
        assert r.status_code == 422 and token not in r.text
    assert issuer.discovery.call_count == 0


def test_pair_account_rate_limited_before_issuer(att_client, issuer):
    for _ in range(10):
        att_client.post("/v1/pair/account", json={"token": "x.y.z"})
    calls = issuer.discovery.call_count
    r = att_client.post("/v1/pair/account", json={"token": issuer.token()})
    assert r.status_code == 429 and issuer.discovery.call_count == calls
    assert r.json()["error"] == "rate_limited"
    # Same budget as /v1/pair.
    assert att_client.post("/v1/pair", json={"code": "12345678"}).status_code == 429


def test_pair_account_device_limit(issuer):
    cfg = account_config("attestation")
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_devices_per_user": 1})})
    with make_client(cfg) as c:
        link(c, issuer.token(), api_token(c))
        assert pair_account(c, issuer.token()).status_code == 200
        r = pair_account(c, issuer.token())
        assert r.status_code == 403 and r.json()["error"] == "limit"


def test_approval_device_limit(issuer):
    cfg = account_config("approval")
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_devices_per_user": 1})})
    with make_client(cfg) as c:
        pat = api_token(c)
        first = pending_request(c, issuer, pat)
        second = pair_account(c, issuer.token()).json()
        assert approve(c, first["request_id"], pat).status_code == 200
        assert poll(c, first["poll_token"]).status_code == 200
        r = approve(c, second["request_id"], pat)
        assert r.status_code == 403 and r.json()["error"] == "limit"
        r = pair_account(c, issuer.token())
        assert r.status_code == 403 and r.json()["error"] == "limit"


def test_approving_before_collecting_cannot_exceed_the_device_limit(issuer):
    cfg = account_config("approval")
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_devices_per_user": 1})})
    with make_client(cfg) as c:
        pat = api_token(c)
        first = pending_request(c, issuer, pat)
        second = pair_account(c, issuer.token()).json()
        assert approve(c, first["request_id"], pat).status_code == 200
        assert approve(c, second["request_id"], pat).status_code == 200
        assert poll(c, first["poll_token"]).status_code == 200
        r = poll(c, second["poll_token"])
        assert r.status_code == 403 and r.json()["error"] == "limit"
        assert len(c.get("/v1/devices", headers=h(pat)).json()["devices"]) == 1
        # Still collectable once a device is revoked.
        device_id = c.get("/v1/devices", headers=h(pat)).json()["devices"][0]["id"]
        assert c.delete(f"/v1/devices/{device_id}", headers=h(pat)).status_code == 204
        r = poll(c, second["poll_token"])
        assert r.status_code == 200 and set(r.json()) == {"device_id", "token"}
        assert len(c.get("/v1/devices", headers=h(pat)).json()["devices"]) == 1


def test_other_users_requests_are_invisible(appr_client, issuer):
    pat_a = api_token(appr_client)
    pat_b = api_token(appr_client, "bob")
    body = pending_request(appr_client, issuer, pat_a)
    assert appr_client.get("/v1/pairing-requests", headers=h(pat_b)).json() == {"requests": []}
    r = approve(appr_client, body["request_id"], pat_b)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert appr_client.post(f"/v1/pairing-requests/{body['request_id']}/deny", headers=h(pat_b)).status_code == 404
    assert poll(appr_client, body["poll_token"]).status_code == 202
    assert len(appr_client.get("/v1/pairing-requests", headers=h(pat_a)).json()["requests"]) == 1


def test_device_token_cannot_approve(appr_client, issuer):
    pat = api_token(appr_client)
    body = pending_request(appr_client, issuer, pat)
    device = device_token(appr_client)
    r = approve(appr_client, body["request_id"], device)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert appr_client.get("/v1/pairing-requests", headers=h(device)).status_code == 403
    assert appr_client.post(f"/v1/pairing-requests/{body['request_id']}/deny", headers=h(device)).status_code == 403
    assert poll(appr_client, body["poll_token"]).status_code == 202


def test_pair_account_never_echoes_or_logs_tokens(appr_client, issuer, caplog):
    caplog.set_level(logging.DEBUG)
    pat = api_token(appr_client)
    tokens = [issuer.token(aud=["other"]), issuer.token(nonce="n-1"), issuer.token(exp=1)]
    responses = [pair_account(appr_client, t) for t in tokens]
    good = issuer.token()
    responses.append(pair_account(appr_client, good))  # not linked yet
    link(appr_client, good, pat)
    pending = pair_account(appr_client, good)
    responses.append(pending)
    assert [r.status_code for r in responses] == [401, 401, 401, 403, 202]
    for r in responses:
        for t in [*tokens, good]:
            assert t not in r.text
    for t in [*tokens, good, pending.json()["poll_token"]]:
        assert t not in caplog.text
    assert f"pairing request for user {run(appr_client.app.state.storage.users.by_handle('owner')).id}" in caplog.text
