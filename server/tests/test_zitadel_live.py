"""Against the real central account issuer. Opt in: WRISTCALL_ZITADEL_E2E_SECRET=... pytest -m zitadel"""

import os

import httpx
import pytest

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
