"""ZitadelUsers against the real Zitadel: creates a throwaway user in the organization, deletes it, deletes it again.

Skipped unless WRISTCALL_CLOUD_TEST_ZITADEL_CLIENT_SECRET (or _FILE) is set; the issuer, organization and machine
user default to wristcall's (README, "Account deletion"). Needs a machine user with ORG_USER_MANAGER there.
"""

import os
import secrets

import httpx
import pytest

from wristcall_cloud.identity import SCOPE, ZitadelUsers

ISSUER = os.environ.get("WRISTCALL_CLOUD_TEST_ZITADEL_ISSUER", "https://auth.trigram.com.br")
ORG = os.environ.get("WRISTCALL_CLOUD_TEST_ZITADEL_ORG_ID", "394311486572333063")
CLIENT_ID = os.environ.get("WRISTCALL_CLOUD_TEST_ZITADEL_CLIENT_ID", "wristcall-cloud")
UA = {"User-Agent": "wristcall-cloud-tests"}


def _secret() -> str | None:
    path = os.environ.get("WRISTCALL_CLOUD_TEST_ZITADEL_CLIENT_SECRET_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get("WRISTCALL_CLOUD_TEST_ZITADEL_CLIENT_SECRET") or None


SECRET = _secret()
pytestmark = pytest.mark.skipif(SECRET is None, reason="WRISTCALL_CLOUD_TEST_ZITADEL_CLIENT_SECRET not set")


async def _admin_headers(http: httpx.AsyncClient) -> dict[str, str]:
    r = await http.post(
        f"{ISSUER}/oauth/v2/token",
        auth=(CLIENT_ID, SECRET),
        data={"grant_type": "client_credentials", "scope": SCOPE},
        headers=UA,
    )
    r.raise_for_status()
    return {**UA, "Authorization": f"Bearer {r.json()['access_token']}", "x-zitadel-orgid": ORG}


async def test_deletes_a_throwaway_user_of_the_organization():
    async with httpx.AsyncClient(timeout=30) as http:
        headers = await _admin_headers(http)
        name = f"wc-cloud-test-{secrets.token_hex(4)}"
        r = await http.post(
            f"{ISSUER}/v2/users/human",
            headers=headers,
            json={
                "organization": {"orgId": ORG},
                "username": name,
                "profile": {"givenName": "Throwaway", "familyName": "Test"},
                "email": {"email": f"{name}@example.invalid", "isVerified": True},
            },
        )
        assert r.status_code in (200, 201), r.status_code
        user_id = r.json()["userId"]
        try:
            users = ZitadelUsers(ISSUER, ORG, CLIENT_ID, SECRET, http)
            assert await users.delete(user_id) is True
            assert (await http.get(f"{ISSUER}/management/v1/users/{user_id}", headers=headers)).status_code == 404
            assert await users.delete(user_id) is False  # already gone: left alone
        finally:
            await http.delete(f"{ISSUER}/management/v1/users/{user_id}", headers=headers)
