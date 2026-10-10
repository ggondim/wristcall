"""ZitadelUsers against a fake Zitadel (respx): client credentials token, then the management API's user deletion."""

import base64

import httpx
import pytest
import respx

from conftest import FakeVerifier
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.identity import IdentityUnavailable, ZitadelUsers

ISSUER = "https://auth.test"
ORG = "org-1"
TOKEN_URL = f"{ISSUER}/oauth/v2/token"
SECRET = "s3cret-value-never-logged"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def token_reply(token: str = "machine-token", expires_in: int = 43199) -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": expires_in})


def user_url(user_id: str) -> str:
    return f"{ISSUER}/management/v1/users/{user_id}"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def users(clock):
    async with httpx.AsyncClient() as http:
        yield ZitadelUsers(ISSUER, ORG, "wristcall-cloud", SECRET, http, now=clock)


@respx.mock
async def test_delete_uses_client_credentials_and_the_org_header(users):
    token = respx.post(TOKEN_URL).mock(return_value=token_reply())
    delete = respx.delete(user_url("42")).mock(return_value=httpx.Response(200, json={}))
    assert await users.delete("42") is True
    sent = token.calls.last.request
    assert sent.headers["authorization"] == "Basic " + base64.b64encode(f"wristcall-cloud:{SECRET}".encode()).decode()
    form = dict(httpx.QueryParams(sent.content.decode()))
    assert form == {"grant_type": "client_credentials", "scope": "openid urn:zitadel:iam:org:project:id:zitadel:aud"}
    call = delete.calls.last.request
    assert call.headers["authorization"] == "Bearer machine-token"
    assert call.headers["x-zitadel-orgid"] == ORG
    assert call.headers["user-agent"].startswith("wristcall-cloud/")


@respx.mock
async def test_user_outside_the_org_or_gone_is_false(users):
    respx.post(TOKEN_URL).mock(return_value=token_reply())
    respx.delete(user_url("42")).mock(return_value=httpx.Response(404, json={"code": 5, "message": "not found"}))
    assert await users.delete("42") is False


@respx.mock
async def test_token_is_reused_until_it_expires(users, clock):
    token = respx.post(TOKEN_URL).mock(side_effect=[token_reply("t1", 3600), token_reply("t2", 3600)])
    delete = respx.delete(url__regex=rf"{ISSUER}/management/v1/users/\d+").mock(return_value=httpx.Response(200))
    await users.delete("1")
    clock.t += 3000
    await users.delete("2")
    assert token.call_count == 1
    clock.t += 600  # within a minute of expiry: a new token
    await users.delete("3")
    assert token.call_count == 2
    assert delete.calls.last.request.headers["authorization"] == "Bearer t2"


@respx.mock
async def test_rejected_token_is_refreshed_once(users):
    token = respx.post(TOKEN_URL).mock(side_effect=[token_reply("old"), token_reply("new")])
    delete = respx.delete(user_url("42")).mock(side_effect=[httpx.Response(401), httpx.Response(200)])
    assert await users.delete("42") is True
    assert token.call_count == 2
    assert delete.calls.last.request.headers["authorization"] == "Bearer new"


@respx.mock
async def test_rejected_twice_is_unavailable(users):
    respx.post(TOKEN_URL).mock(return_value=token_reply())
    respx.delete(user_url("42")).mock(return_value=httpx.Response(401))
    with pytest.raises(IdentityUnavailable):
        await users.delete("42")


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(403, json={"code": 7, "message": "No matching permissions found"}),
        httpx.Response(500),
        httpx.Response(503),
        httpx.ConnectError("boom"),
    ],
)
@respx.mock
async def test_management_failures_are_unavailable(users, reply):
    respx.post(TOKEN_URL).mock(return_value=token_reply("tok-should-not-leak"))
    route = respx.delete(user_url("42"))
    if isinstance(reply, Exception):
        route.mock(side_effect=reply)
    else:
        route.mock(return_value=reply)
    with pytest.raises(IdentityUnavailable) as e:
        await users.delete("42")
    assert SECRET not in str(e.value) and "tok-should-not-leak" not in str(e.value)


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(401, json={"error": "invalid_client"}),
        httpx.Response(200, json={"token_type": "Bearer"}),
        httpx.Response(200, text="not json"),
        httpx.ConnectError("boom"),
    ],
)
@respx.mock
async def test_token_failures_are_unavailable(users, reply):
    route = respx.post(TOKEN_URL)
    if isinstance(reply, Exception):
        route.mock(side_effect=reply)
    else:
        route.mock(return_value=reply)
    delete = respx.delete(user_url("42")).mock(return_value=httpx.Response(200))
    with pytest.raises(IdentityUnavailable) as e:
        await users.delete("42")
    assert SECRET not in str(e.value)
    assert delete.call_count == 0


@pytest.mark.parametrize("user_id", ["", "a/../orgs", "42?x=1", "4 2", "x" * 201])
@respx.mock
async def test_a_subject_that_is_no_user_id_is_not_sent(users, user_id):
    token = respx.post(TOKEN_URL).mock(return_value=token_reply())
    assert await users.delete(user_id) is False
    assert token.call_count == 0


def test_app_deletes_users_only_with_the_machine_credentials():
    base = {"mongo_url": "mongodb://localhost:1", "issuer": ISSUER, "clients": {"ios": "client-ios"}}
    without = create_app(CloudConfig(**base), verifier=FakeVerifier())
    assert without.state.users is None
    config = CloudConfig(**base, zitadel_org_id=ORG, zitadel_client_id="wristcall-cloud", zitadel_client_secret=SECRET)
    assert isinstance(create_app(config, verifier=FakeVerifier()).state.users, ZitadelUsers)
