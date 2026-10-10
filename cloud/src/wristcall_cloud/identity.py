"""Deletes an account's user at the identity provider (Zitadel) when the account is deleted (decision 23).

The Cloud signs in as a machine user of the provider (client credentials) that manages the users of one
organization (role ORG_USER_MANAGER) and calls the management API scoped to that organization. A user of another
organization, or one already deleted, is "not found" there and is left alone: the Cloud deletes only users of its
own organization. Neither the client secret nor the machine token ever reaches a log or an exception message.
"""

import re
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from . import __version__

# Zitadel's management API answers to tokens meant for its own project.
SCOPE = "openid urn:zitadel:iam:org:project:id:zitadel:aud"
_USER_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_REFRESH_BEFORE_S = 60.0  # a cached token this close to its expiry is replaced


class IdentityUnavailable(Exception):
    """The provider could not be reached, refused the machine user, or failed: the user was not deleted."""


class ZitadelUsers:
    def __init__(
        self,
        issuer: str,
        org_id: str,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._org_id = org_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._now = now
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._headers = {"User-Agent": f"wristcall-cloud/{__version__}", "Accept": "application/json"}

    async def delete(self, user_id: str) -> bool:
        """Deletes the user: True if deleted, False if not a user of the organization (or already gone).
        Raises IdentityUnavailable otherwise."""
        if not _USER_ID.fullmatch(user_id):
            return False  # not a provider user id: nothing of the organization to delete
        url = f"{self._issuer}/management/v1/users/{quote(user_id, safe='')}"
        for attempt in range(2):
            token = await self._access_token()
            try:
                r = await self._http.delete(
                    url,
                    headers={**self._headers, "Authorization": f"Bearer {token}", "x-zitadel-orgid": self._org_id},
                    timeout=15.0,
                )
            except httpx.HTTPError as e:
                raise IdentityUnavailable(f"user deletion failed: {type(e).__name__}") from None
            if r.status_code == 401 and attempt == 0:
                self._token = None  # revoked or rotated: one new token, then give up
                continue
            if r.status_code == 404:
                return False
            if r.is_success:
                return True
            raise IdentityUnavailable(f"user deletion answered {r.status_code}")
        raise IdentityUnavailable("user deletion answered 401")

    async def _access_token(self) -> str:
        if self._token is not None and self._now() < self._token_expires_at - _REFRESH_BEFORE_S:
            return self._token
        self._token = None
        try:
            r = await self._http.post(
                f"{self._issuer}/oauth/v2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials", "scope": SCOPE},
                headers=self._headers,
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            raise IdentityUnavailable(f"machine token request failed: {type(e).__name__}") from None
        if not r.is_success:
            raise IdentityUnavailable(f"machine token request answered {r.status_code}")
        try:
            body = r.json()
            token, expires_in = body["access_token"], float(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError):
            raise IdentityUnavailable("machine token reply is not usable") from None
        if not isinstance(token, str) or not token:
            raise IdentityUnavailable("machine token reply is not usable")
        self._token, self._token_expires_at = token, self._now() + expires_in
        return token
