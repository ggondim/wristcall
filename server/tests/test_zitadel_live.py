"""Against the real central account issuer. Opt in: WRISTCALL_ZITADEL_E2E_SECRET=... pytest -m zitadel

The verifier still checks the issuer's own access tokens (the Cloud does that), but a 0.6.0 server takes only
per-server tokens from the Cloud: Zitadel does not issue tokens whose audience is a server URL (no RFC 8707), so
its access token presented directly to a server is a 401.
"""

import asyncio
import os

import httpx
import pytest

from conftest import fake_config
from test_api import make_client
from wristcall.config import CentralAccountConfig
from wristcall.oidc import OidcError, OidcVerifier

pytestmark = pytest.mark.zitadel

ISSUER = os.environ.get("WRISTCALL_ZITADEL_ISSUER", "https://auth.trigram.com.br")
CLIENT = os.environ.get("WRISTCALL_ZITADEL_E2E_CLIENT", "wristcall-e2e")


async def _token(http: httpx.AsyncClient) -> str:
    secret = os.environ.get("WRISTCALL_ZITADEL_E2E_SECRET")
    if not secret:
        pytest.skip("WRISTCALL_ZITADEL_E2E_SECRET not set")
    r = await http.post(
        f"{ISSUER}/oauth/v2/token",
        auth=(CLIENT, secret),
        data={"grant_type": "client_credentials", "scope": "openid"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def test_real_token_verifies():
    async with httpx.AsyncClient() as http:
        identity = await OidcVerifier(ISSUER, [CLIENT], http).verify(await _token(http))
    assert identity.issuer == ISSUER and identity.client_id == CLIENT and identity.subject


async def test_real_token_for_another_audience_is_rejected():
    async with httpx.AsyncClient() as http:
        token = await _token(http)
        with pytest.raises(OidcError):
            await OidcVerifier(ISSUER, ["not-this-client"], http).verify(token)


def test_real_token_presented_to_a_server_is_rejected():
    if not os.environ.get("WRISTCALL_ZITADEL_E2E_SECRET"):
        pytest.skip("WRISTCALL_ZITADEL_E2E_SECRET not set")

    async def fetch() -> str:
        async with httpx.AsyncClient() as http:
            return await _token(http)

    token = asyncio.run(fetch())
    # Even a server that (wrongly) names Zitadel as its issuer: no per-server audience, no wc-server+jwt type.
    central = CentralAccountConfig(
        issuer=ISSUER, audience=["https://wc.example.test"], clients=[CLIENT], device_credential="attestation"
    )
    with make_client(fake_config().model_copy(update={"central_account": central})) as c:
        r = c.post("/v1/pair/account", json={"token": token, "device_name": "watch"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_account_token"
