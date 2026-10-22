"""The node's HTTP client for the Cloud's `/v1/node/*` API, the account proof and the in-process fake Cloud."""

import logging
import time

import httpx
import pytest
import respx
from fake_cloud import CLOUD_URL, FULL_PLAN, FakeCloud

from wristcall.cloud_node.client import CloudClient, CloudResponse, raise_for_account
from wristcall.cloud_node.proof import ProofMismatch, UserProof, current_proof, require_proof
from wristcall.oidc import OidcVerifier
from wristcall.storage import CallOpen, CloudUnavailable, Conflict, DeviceRevoked, PlanRequired, StorageError

SECRET = "wc_node_" + "s" * 43
NODE_URL = "https://node.test"
PROOF = UserProof(account="acc-1", device="dev-1", platform="ios", token="user-token-value")


def test_new_errors_are_storage_errors() -> None:
    assert issubclass(CloudUnavailable, StorageError)
    assert issubclass(PlanRequired, StorageError)
    assert issubclass(DeviceRevoked, StorageError)
    assert issubclass(CallOpen, Conflict)


@respx.mock
async def test_sends_node_secret_and_proofs() -> None:
    route = respx.get(f"{CLOUD_URL}/v1/node/me").mock(return_value=httpx.Response(200, json={"ok": True}))
    client = CloudClient(CLOUD_URL, SECRET, httpx.AsyncClient())

    resp = await client.request("GET", "/v1/node/me")
    sent = route.calls.last.request.headers
    assert sent["authorization"] == f"Bearer {SECRET}"
    assert "x-wristcall-user-token" not in sent and "x-wristcall-call-grant" not in sent
    assert resp == CloudResponse(status=200, body={"ok": True}, headers=resp.headers)
    assert resp.code is None

    await client.request("GET", "/v1/node/me", user=PROOF)
    sent = route.calls.last.request.headers
    assert sent["x-wristcall-user-token"] == "user-token-value"
    assert "x-wristcall-call-grant" not in sent

    await client.request("GET", "/v1/node/me", grant="grant-1", headers={"If-None-Match": '"e"'})
    sent = route.calls.last.request.headers
    assert sent["x-wristcall-call-grant"] == "grant-1"
    assert sent["if-none-match"] == '"e"'
    assert "x-wristcall-user-token" not in sent


@respx.mock
async def test_sends_json_body_and_repeated_params() -> None:
    route = respx.post(f"{CLOUD_URL}/v1/node/calls").mock(return_value=httpx.Response(204))
    client = CloudClient(CLOUD_URL + "/", SECRET, httpx.AsyncClient())
    resp = await client.request(
        "POST", "/v1/node/calls", json={"a": 1}, params=[("terms", "x1"), ("terms", "x2")]
    )
    request = route.calls.last.request
    assert request.url.params.get_list("terms") == ["x1", "x2"]
    assert request.content == b'{"a":1}' or request.content == b'{"a": 1}'
    assert resp.status == 204 and resp.body is None


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(502, text="<html>bad gateway</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
        httpx.Response(503, json={"error": "unavailable", "message": "down"}),
    ],
    ids=["connect", "timeout", "502-html", "200-html", "200-bad-json", "503-json"],
)
@respx.mock
async def test_transport_errors_are_cloud_unavailable(outcome, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    route = respx.get(f"{CLOUD_URL}/v1/node/me")
    if isinstance(outcome, Exception):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=outcome)
    client = CloudClient(CLOUD_URL, SECRET, httpx.AsyncClient())
    with pytest.raises(CloudUnavailable) as raised:
        await client.request("GET", "/v1/node/me", user=PROOF, grant="grant-secret")
    for secret in (SECRET, "user-token-value", "grant-secret"):
        assert secret not in str(raised.value)
        assert secret not in caplog.text


@respx.mock
async def test_4xx_is_returned_with_code() -> None:
    respx.post(f"{CLOUD_URL}/v1/node/agents").mock(
        return_value=httpx.Response(409, json={"error": "conflict", "message": "taken"})
    )
    client = CloudClient(CLOUD_URL, SECRET, httpx.AsyncClient())
    resp = await client.request("POST", "/v1/node/agents", user=PROOF, json={})
    assert resp.status == 409
    assert resp.code == "conflict"
    assert CloudResponse(status=200, body=[1], headers=httpx.Headers()).code is None


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (402, "plan_required", PlanRequired),
        (403, "device_revoked", DeviceRevoked),
        (401, "unauthorized", CloudUnavailable),
        (429, "rate_limited", CloudUnavailable),
        (402, "quota_exceeded", None),
        (403, "forbidden", None),
        (404, "not_found", None),
        (200, None, None),
    ],
)
def test_raise_for_account(status, code, error, caplog) -> None:
    body = {"error": code, "message": "m"} if code else {}
    resp = CloudResponse(status=status, body=body, headers=httpx.Headers())
    if error is None:
        raise_for_account(resp)
        return
    with caplog.at_level(logging.ERROR, logger="wristcall.cloud_node.client"):
        with pytest.raises(error):
            raise_for_account(resp)
    if status == 401:
        assert "refused" in caplog.text


@respx.mock
async def test_stream_lines_needs_end_line() -> None:
    route = respx.get(f"{CLOUD_URL}/v1/node/calls/export")
    route.mock(
        return_value=httpx.Response(
            200, content=b'{"call": {"id": "c_1"}, "entries": []}\n{"call": {"id": "c_2"}, "entries": []}\n',
            headers={"content-type": "application/x-ndjson"},
        )
    )
    client = CloudClient(CLOUD_URL, SECRET, httpx.AsyncClient())
    seen = []
    with pytest.raises(CloudUnavailable):
        async for line in client.stream_lines("/v1/node/calls/export", user=PROOF, params=[]):
            seen.append(line["call"]["id"])
    assert seen == ["c_1", "c_2"]
    assert route.calls.last.request.headers["x-wristcall-user-token"] == "user-token-value"

    route.mock(
        return_value=httpx.Response(
            200, content=b'{"call": {"id": "c_1"}, "entries": []}\n{"end": true, "count": 1}\n',
            headers={"content-type": "application/x-ndjson"},
        )
    )
    lines = [line async for line in client.stream_lines("/v1/node/calls/export", user=PROOF, params=[("agent", "a")])]
    assert lines == [{"call": {"id": "c_1"}, "entries": []}]
    assert route.calls.last.request.url.params["agent"] == "a"


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(200, content=b"<html>\n", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"[1]\n", headers={"content-type": "application/x-ndjson"}),
        httpx.Response(200, content=b'{"end": true, "count": 3}\n', headers={"content-type": "application/x-ndjson"}),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(401, json={"error": "unauthorized", "message": "m"}),
        httpx.ConnectError("refused"),
    ],
    ids=["html", "not-object", "count-mismatch", "502", "401", "connect"],
)
@respx.mock
async def test_stream_lines_bad_answers(outcome) -> None:
    route = respx.get(f"{CLOUD_URL}/v1/node/calls/export")
    if isinstance(outcome, Exception):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=outcome)
    client = CloudClient(CLOUD_URL, SECRET, httpx.AsyncClient())
    with pytest.raises(CloudUnavailable):
        _ = [line async for line in client.stream_lines("/v1/node/calls/export", user=PROOF, params=[])]


@respx.mock
async def test_stream_lines_device_revoked() -> None:
    respx.get(f"{CLOUD_URL}/v1/node/calls/export").mock(
        return_value=httpx.Response(403, json={"error": "device_revoked", "message": "m"})
    )
    client = CloudClient(CLOUD_URL, SECRET, httpx.AsyncClient())
    with pytest.raises(DeviceRevoked):
        _ = [line async for line in client.stream_lines("/v1/node/calls/export", user=PROOF, params=[])]


def test_require_proof() -> None:
    assert current_proof.get() is None
    with pytest.raises(PermissionError):
        require_proof("acc-1")
    token = current_proof.set(PROOF)
    try:
        assert require_proof("acc-1") is PROOF
        with pytest.raises(ProofMismatch):
            require_proof("acc-2")
    finally:
        current_proof.reset(token)
    assert issubclass(ProofMismatch, PermissionError)
    assert "user-token-value" not in repr(PROOF)


# --- the fake Cloud ---------------------------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeCloud:
    return FakeCloud(NODE_URL, SECRET)


def proof(fake: FakeCloud, account: str = "acc-1", device: str = "dev-1", platform: str = "ios", **claims) -> UserProof:
    return UserProof(account, device, platform, fake.user_token(account, device, platform, **claims))


async def test_fake_cloud_contract_smoke(fake: FakeCloud) -> None:
    client = CloudClient(CLOUD_URL, SECRET, fake.http())
    ok = await client.request("GET", "/v1/node/me", user=proof(fake))
    assert ok.status == 200

    other_node = await client.request("GET", "/v1/node/me", user=proof(fake, aud="https://other.test"))
    assert (other_node.status, other_node.code) == (401, "unauthorized")
    for dropped in ("did", "plt", "sub"):
        missing = await client.request("GET", "/v1/node/me", user=proof(fake, **{dropped: None}))
        assert missing.status == 401, dropped
    expired = await client.request("GET", "/v1/node/me", user=proof(fake, exp=1000, iat=900))
    assert expired.status == 401
    wrong_typ = await client.request("GET", "/v1/node/me", user=proof(fake, typ="at+jwt"))
    assert wrong_typ.status == 401
    bad_secret = await CloudClient(CLOUD_URL, "wc_node_" + "x" * 43, fake.http()).request(
        "GET", "/v1/node/me", user=proof(fake)
    )
    assert (bad_secret.status, bad_secret.code) == (401, "unauthorized")

    fake.revoked.add("dev-1")
    revoked = await client.request("GET", "/v1/node/me", user=proof(fake))
    assert (revoked.status, revoked.code) == (403, "device_revoked")
    fake.revoked.clear()

    fake.retry_after = 7
    limited = await client.request("GET", "/v1/node/me", user=proof(fake))
    assert (limited.status, limited.code, limited.headers["retry-after"]) == (429, "rate_limited", "7")
    fake.retry_after = None

    fake.down = True
    async with fake.http() as http:
        for path in ("/v1/node/me", "/v1/jwks", "/.well-known/openid-configuration"):
            assert (await http.get(path)).status_code == 503
    with pytest.raises(CloudUnavailable):
        await client.request("GET", "/v1/node/me", user=proof(fake))


async def test_fake_cloud_tokens_pass_the_server_verifier(fake: FakeCloud) -> None:
    verifier = OidcVerifier(CLOUD_URL, [NODE_URL], fake.http(), typ="wc-server+jwt")
    identity = await verifier.verify(fake.user_token("acc-1", "dev-1", "watchos"))
    assert (identity.issuer, identity.subject, identity.did, identity.plt) == (CLOUD_URL, "acc-1", "dev-1", "watchos")
    assert identity.jti and identity.expires_at - identity.issued_at == 300


AGENT_KEYS = {"id", "slug", "display_name", "icon", "color", "call_type", "position", "spec", "created_at", "updated_at"}
CALL_KEYS = {
    "id", "agent_id", "agent_slug", "agent_name", "device_id", "call_type", "status", "error", "attempts",
    "last_http_status", "delivery", "created_at", "updated_at", "ended_at", "finished_at", "expires_at", "seconds",
    "open",
}
ENTRY_KEYS = {"seq", "role", "text", "sealed", "error", "at"}


async def test_fake_cloud_agents_and_calls(fake: FakeCloud) -> None:
    client = CloudClient(CLOUD_URL, SECRET, fake.http())
    user, watch = proof(fake), proof(fake, device="dev-w", platform="watchos")
    agent = {"id": "ag_0123456789ab", "slug": "helper", "display_name": "Helper", "icon": "star", "call_type": "one-shot",
             "spec": {"retention_days": 7}}

    assert (await client.request("POST", "/v1/node/agents", user=user, json=agent)).code == "plan_required"
    fake.plans["acc-1"] = FULL_PLAN
    assert (await client.request("POST", "/v1/node/agents", user=watch, json=agent)).code == "forbidden"
    created = await client.request("POST", "/v1/node/agents", user=user, json=agent)
    assert created.status == 201 and set(created.body) == AGENT_KEYS and created.body["color"] is None
    assert (await client.request("POST", "/v1/node/agents", user=user, json=agent)).code == "conflict"
    bad = await client.request("POST", "/v1/node/agents", user=user, json={**agent, "id": "nope"})
    assert (bad.status, bad.code) == (422, "invalid")

    me = await client.request("GET", "/v1/node/me", user=user)
    assert set(me.body) == {"account", "plan", "device", "agents", "usage"}
    assert me.body["plan"] == FULL_PLAN and me.body["device"] == {"id": "dev-1", "platform": "ios"}
    assert set(me.body["usage"]) == {"month", "used_s", "quota_s", "remaining_s"}

    listing = await client.request("GET", "/v1/node/agents", user=user)
    assert listing.headers["etag"] == listing.body["etag"]
    cached = await client.request("GET", "/v1/node/agents", user=user, headers={"If-None-Match": listing.body["etag"]})
    assert cached.status == 304 and cached.body is None
    ordered = await client.request("PUT", "/v1/node/agents/order", user=user, json={"agent": agent["id"], "index": 3})
    assert ordered.status == 200 and set(ordered.body) == {"agents", "etag"}
    patched = await client.request("PATCH", f"/v1/node/agents/{agent['id']}", user=user, json={"display_name": "H2"})
    assert patched.status == 200 and patched.body["display_name"] == "H2"
    assert (await client.request("GET", "/v1/node/agents/helper", user=user)).body["id"] == agent["id"]

    body = {"agent": "helper", "call_type": "one-shot", "request_id": "req-0001", "delivery": "history"}
    assert (await client.request("POST", "/v1/node/calls", user=user, json={**body, "call_type": "monologue"})).status == 422
    opened = await client.request("POST", "/v1/node/calls", user=user, json=body)
    assert opened.status == 201
    assert set(opened.body) == {"call_id", "grant", "agent", "plan", "quota_s", "max_s", "started_at"}
    assert opened.body["max_s"] == fake.max_one_way_s and opened.body["quota_s"] == fake.quota_s
    call_id, grant = opened.body["call_id"], opened.body["grant"]
    replay = await client.request("POST", "/v1/node/calls", user=user, json=body)
    assert replay.status == 200 and replay.body["call_id"] == call_id and replay.body["grant"] != grant
    busy = await client.request("POST", "/v1/node/calls", user=user, json={**body, "request_id": "req-0002"})
    assert (busy.status, busy.code) == (409, "too_many_calls")

    path = f"/v1/node/calls/{call_id}"
    entry = {"seq": 0, "role": "user", "text": "v1.sealed", "sealed": True, "error": None, "at": 1.0,
             "term_hashes": ["x0123456789abcdef"]}
    assert (await client.request("POST", f"{path}/entries", grant=grant, json={"entries": [entry]})).status == 204
    clear = {**entry, "seq": 1, "sealed": False}
    assert (await client.request("POST", f"{path}/entries", grant=grant, json={"entries": [clear]})).status == 422
    assert (await client.request("POST", f"{path}/entries", grant="grant-x", json={"entries": []})).status == 401
    assert (await client.request("PATCH", path, grant=grant, json={"status": "processing"})).status == 204
    assert (await client.request("DELETE", path, user=user)).code == "call_open"
    pushed = await client.request("POST", "/v1/node/push", grant=grant, json={"event": "call.delivered", "title": "t", "call": call_id})
    assert (pushed.status, pushed.body) == (202, {"sent": 0})
    finish = {"status": "delivered", "seconds": 4.2}
    assert (await client.request("POST", f"{path}/finish", grant=grant, json=finish)).status == 204
    assert (await client.request("POST", f"{path}/finish", grant=grant, json=finish)).status == 204
    assert fake.used_s["acc-1"] == 5

    read = await client.request("GET", path, user=user)
    assert set(read.body) == {"call", "entries"} and set(read.body["call"]) == CALL_KEYS
    assert set(read.body["entries"][0]) == ENTRY_KEYS
    assert read.body["call"]["open"] is False and read.body["call"]["delivery"] == "history"
    assert (await client.request("GET", path, user=watch)).status == 404
    assert (await client.request("GET", "/v1/node/calls", user=watch)).code == "forbidden"
    found = await client.request("GET", "/v1/node/calls", user=user, params=[("terms", "x0123456789abcdef")])
    assert [c["id"] for c in found.body["calls"]] == [call_id]
    missed = await client.request("GET", "/v1/node/calls", user=user, params=[("terms", "xffffffffffffffff")])
    assert missed.body == {"calls": []}
    lines = [line async for line in client.stream_lines("/v1/node/calls/export", user=user, params=[])]
    assert [line["call"]["id"] for line in lines] == [call_id]

    assert (await client.request("PUT", "/v1/node/push-targets", user=user, json={"push_key": "wc_push_" + "k" * 43})).status == 204
    pushed = await client.request("POST", "/v1/node/push", user=user, json={"event": "test", "title": "t"})
    assert pushed.body == {"sent": 1} and fake.pushes[-1]["device"] == "dev-1"
    beat = {"boot_id": "b1", "version": "0.10.0", "active_call_ids": [], "active_by_type": {"conversation": 0, "one_way": 0},
            "capacity": {"conversation": 3, "one_way": None}, "providers_ok": True}
    assert (await client.request("POST", "/v1/node/heartbeat", json=beat)).status == 204
    assert fake.heartbeats == [beat]
    assert (await client.request("DELETE", "/v1/node/calls", user=user)).status == 422
    assert (await client.request("DELETE", "/v1/node/calls", user=user, params=[("all", "true")])).body == {"deleted": 1}


async def test_fake_cloud_expired_plan_is_no_plan(fake: FakeCloud) -> None:
    # The real Cloud keeps only entitlements without `expires_at` or with one still ahead.
    client = CloudClient(CLOUD_URL, SECRET, fake.http())
    fake.plans["acc-1"] = {"plan": "full", "source": "beta", "expires_at": time.time() - 60}
    me = await client.request("GET", "/v1/node/me", user=proof(fake))
    assert (me.status, me.body["plan"]) == (200, None)
    agent = {"id": "ag_0123456789ab", "slug": "helper", "display_name": "Helper", "icon": "star",
             "call_type": "one-shot", "spec": {}}
    created = await client.request("POST", "/v1/node/agents", user=proof(fake), json=agent)
    assert (created.status, created.code) == (402, "plan_required")
    fake.plans["acc-1"] = {"plan": "full", "source": "beta", "expires_at": time.time() + 3600}
    me = await client.request("GET", "/v1/node/me", user=proof(fake))
    assert me.body["plan"]["plan"] == "full"
    assert (await client.request("POST", "/v1/node/agents", user=proof(fake), json=agent)).status == 201


@pytest.mark.parametrize("params", [
    {"before_at": "soon", "before_id": "c_0000000000000000"},
    {"before_at": "nan", "before_id": "c_0000000000000000"},
    {"before_at": "inf", "before_id": "c_0000000000000000"},
    {"limit": "²"},  # a digit to str.isdigit, not to int()
    {"limit": "١"},  # an Arabic-Indic one: int() takes it, the Cloud does not
    {"limit": "0050"},
    {"since": "nan"},
])
async def test_fake_cloud_bad_listing_params_are_422(fake: FakeCloud, params) -> None:
    client = CloudClient(CLOUD_URL, SECRET, fake.http())
    fake.plans["acc-1"] = FULL_PLAN
    resp = await client.request("GET", "/v1/node/calls", user=proof(fake), params=params)
    assert (resp.status, resp.code) == (422, "invalid")
