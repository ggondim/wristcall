"""Linking a local user to the central account: POST/DELETE /v1/account/link and /v1/health."""

import logging

import httpx
import pytest
import respx

from oidc_fixtures import AUDIENCE, ISSUER, SERVER_AUDIENCE, SERVER_TOKEN_TYPE, FakeIssuer
from test_api import api_token, device_token, h, make_client, run
from conftest import fake_config
from wristcall.config import CentralAccountConfig

KEY = f"{ISSUER}#central-user-1"


def central_config():
    return fake_config().model_copy(
        update={"central_account": CentralAccountConfig(issuer=ISSUER, audience=[SERVER_AUDIENCE], clients=[AUDIENCE])}
    )


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def issuer(router):
    # Plays the Cloud: per-server tokens for this server.
    return FakeIssuer(router, aud=[SERVER_AUDIENCE], typ=SERVER_TOKEN_TYPE)


@pytest.fixture
def client(router):
    with make_client(central_config()) as c:
        yield c


def link(client, token, pat=None, code=None):
    body = {"token": token} if code is None else {"token": token, "code": code}
    return client.post("/v1/account/link", json=body, headers=h(pat) if pat else {})


def new_code(client, pat):
    return client.post("/v1/pairing-codes", headers=h(pat)).json()["code"]


def linked_user(client):
    return run(client.app.state.storage.users.by_central(KEY))


def test_health_reports_account(client):
    body = client.get("/v1/health").json()
    assert body["account"] == {"issuer": ISSUER, "device_credential": "approval"}
    assert body["status"] == "ok" and "version" in body and "protocol" in body


def test_health_account_null_without_config():
    with make_client() as c:
        body = c.get("/v1/health").json()
    assert body["account"] is None and body["status"] == "ok"


def test_link_with_pat(client, issuer):
    pat = api_token(client)
    r = link(client, issuer.token(), pat)
    assert r.status_code == 200
    assert r.json() == {"linked": True, "issuer": ISSUER}
    owner = run(client.app.state.storage.users.by_handle("owner"))
    assert linked_user(client).id == owner.id


def test_link_with_code_returns_api_token(client, issuer):
    code = new_code(client, api_token(client))
    r = link(client, issuer.token(), code=code)
    assert r.status_code == 200
    body = r.json()
    owner = run(client.app.state.storage.users.by_handle("owner"))
    assert body["linked"] is True and body["issuer"] == ISSUER
    assert body["user"] == {"id": owner.id, "handle": "owner"}
    assert body["api_token"].startswith("wc_pat_")
    assert linked_user(client).id == owner.id
    agents = client.get("/v1/agents", headers=h(body["api_token"]))
    assert agents.status_code == 200 and [a["slug"] for a in agents.json()["agents"]] == ["default"]
    names = [t.name for t in run(client.app.state.storage.tokens.list(owner.id))]
    assert "account link" in names
    # The code was consumed: it no longer pairs a device.
    assert client.post("/v1/pair", json={"code": code, "device_name": "Watch"}).status_code == 401


def test_link_with_wrong_code(client, issuer):
    code = new_code(client, api_token(client))
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(5):
        r = link(client, issuer.token(), code=wrong)
        assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    # Five wrong tries burn the active codes, as on /v1/pair.
    r = link(client, issuer.token(), code=code)
    assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    assert linked_user(client) is None


def test_link_with_malformed_code_counts_as_wrong(client, issuer):
    code = new_code(client, api_token(client))
    for _ in range(5):
        assert link(client, issuer.token(), code="not a code").json()["error"] == "invalid_code"
    assert link(client, issuer.token(), code=code).status_code == 401


def test_bad_account_token_does_not_spend_the_code(client, issuer):
    code = new_code(client, api_token(client))
    bad = link(client, issuer.token(aud=["other"]), code=code)
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_account_token"
    good = link(client, issuer.token(), code=code)
    assert good.status_code == 200 and good.json()["api_token"].startswith("wc_pat_")


def test_link_requires_a_local_proof(client, issuer):
    api_token(client)
    r = link(client, issuer.token())
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    r = link(client, issuer.token(), device_token(client))
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    # The central account token is not a local proof either.
    r = link(client, issuer.token(), issuer.token())
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert linked_user(client) is None


def test_link_bad_body(client, issuer):
    pat = api_token(client)
    for body in ({}, {"token": 1}, {"token": ""}, {"token": issuer.token(), "code": 12345678}):
        r = client.post("/v1/account/link", json=body, headers=h(pat))
        assert r.status_code == 422 and r.json()["error"] == "invalid"
    r = client.post("/v1/account/link", content=b"[", headers={**h(pat), "Content-Type": "application/json"})
    assert r.status_code == 422


def test_link_conflict_between_users(client, issuer):
    assert link(client, issuer.token(), api_token(client)).status_code == 200
    r = link(client, issuer.token(), api_token(client, "bob"))
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert linked_user(client).handle == "owner"


def test_relink_same_user_is_idempotent(client, issuer):
    pat = api_token(client)
    assert link(client, issuer.token(), pat).status_code == 200
    assert link(client, issuer.token(), pat).status_code == 200
    assert linked_user(client).handle == "owner"


def test_link_not_configured():
    with make_client() as c:
        pat = api_token(c)
        r = c.post("/v1/account/link", json={"token": "x"}, headers=h(pat))
        assert r.status_code == 404 and r.json()["error"] == "not_configured"
        r = c.delete("/v1/account/link", headers=h(pat))
        assert r.status_code == 404 and r.json()["error"] == "not_configured"
        assert c.app.state.account is None


def test_account_token_is_not_a_management_credential(client, issuer):
    pat = api_token(client)
    token = issuer.token()
    assert client.post("/v1/account/link", json={"token": token}, headers=h(pat)).status_code == 200
    calls = issuer.discovery.call_count
    assert client.get("/v1/agents", headers=h(token)).status_code == 401
    assert issuer.discovery.call_count == calls


def test_id_token_cannot_link(client, issuer):
    r = link(client, issuer.token(nonce="n-1"), api_token(client))
    assert r.status_code == 401 and r.json()["error"] == "invalid_account_token"
    assert linked_user(client) is None


def test_issuer_down_is_503(client, issuer):
    issuer.discovery.mock(side_effect=lambda request: httpx.Response(503))
    code = new_code(client, api_token(client))
    r = link(client, issuer.token(), code=code)
    assert r.status_code == 503 and r.json()["error"] == "account_unavailable"
    # Nothing was spent: once the issuer is back (and the refetch delay passed) the code still works.
    assert run(client.app.state.storage.pairing.claim_code(code, 0, 5)) is not None


def test_unlink(client, issuer):
    pat = api_token(client)
    assert link(client, issuer.token(), pat).status_code == 200
    assert client.delete("/v1/account/link", headers=h(pat)).status_code == 204
    assert linked_user(client) is None
    r = client.delete("/v1/account/link", headers=h(pat))
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_unlink_needs_a_pat(client, issuer):
    api_token(client)
    assert client.delete("/v1/account/link").status_code == 401
    assert client.delete("/v1/account/link", headers=h(device_token(client))).status_code == 403
    assert client.delete("/v1/account/link", headers=h(issuer.token())).status_code == 401


def test_link_rate_limited(client, issuer):
    pat = api_token(client)
    for _ in range(10):
        assert link(client, "not-a-jwt", pat).status_code == 401
    r = link(client, issuer.token(), pat)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert issuer.discovery.call_count == 0 and issuer.jwks.call_count == 0
    # Same budget as /v1/pair.
    assert client.post("/v1/pair", json={"code": "12345678"}).status_code == 429


def test_errors_do_not_echo_the_token(client, issuer, caplog):
    caplog.set_level(logging.DEBUG)
    pat = api_token(client)
    code = new_code(client, pat)
    tokens = [issuer.token(aud=["other"]), issuer.token(nonce="n-1"), issuer.token(exp=1), issuer.token()]
    responses = [link(client, t, pat) for t in tokens[:3]]
    responses.append(link(client, tokens[3], code="99999999" if code != "99999999" else "88888888"))
    responses.append(link(client, tokens[3]))
    assert link(client, tokens[3], pat).status_code == 200
    responses.append(link(client, tokens[3], api_token(client, "bob")))
    assert [r.status_code for r in responses] == [401, 401, 401, 401, 401, 409]
    for r in responses:
        for t in tokens:
            assert t not in r.text
        assert code not in r.text
    for t in tokens:
        assert t not in caplog.text
    assert code not in caplog.text
