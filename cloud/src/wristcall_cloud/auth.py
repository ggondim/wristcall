"""Who is calling: the account behind the bearer token.

The account access token (`wc-account+jwt`, issued per device) works on every route; each request checks that its
device still exists, so a revoked device stops at once. A Zitadel token works on the routes that existed before the
Cloud had its own sessions only while the transition flag is on (WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS), for the app
builds that predate them. Tokens never reach a log, a response or an exception message.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import Header, Request

from .account_store import AccountStore
from .api_common import ApiError
from .config import CloudConfig
from .oidc import Identity, OidcError, OidcUnavailable
from .session_tokens import AccountTokens, TokenError

log = logging.getLogger("wristcall_cloud.auth")

# The OIDC client each platform's app signs in with (per-server tokens name it), by the key in config.clients.
_PLATFORM_CLIENT = {"ios": "ios", "ipados": "ios", "macos": "ios", "watchos": "watch", "web": "pwa"}
_CLIENT_PLATFORM = {"ios": "ios", "watch": "watchos", "pwa": "web"}
_LEAST_PRIVILEGED = "watchos"


class Verifier(Protocol):
    async def verify(self, token: str) -> Identity: ...


@dataclass(frozen=True)
class Caller:
    key: str  # the account id
    client_id: str | None  # the OIDC client per-server tokens name
    platform: str  # one of PLATFORMS
    device: str | None  # the device id; None for a Zitadel token (transition flag)


def platform_client(platform: str, clients: Mapping[str, str]) -> str | None:
    """The OIDC client id of the app on `platform`; None when that client is not configured."""
    name = _PLATFORM_CLIENT.get(platform)
    return clients.get(name) if name is not None else None


def client_platform(client_id: str | None, clients: Mapping[str, str]) -> str:
    """The platform of the app a Zitadel token was issued to; an unknown client counts as the watch (least
    privileged)."""
    if client_id is not None:
        for name, platform in _CLIENT_PLATFORM.items():
            if clients.get(name) == client_id:
                return platform
    return _LEAST_PRIVILEGED


def _unauthorized(message: str = "missing or invalid token") -> ApiError:
    return ApiError("unauthorized", message, 401)


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    return token


async def zitadel_identity(request: Request, token: str) -> Identity:
    """The Zitadel user behind `token` (503 when the issuer cannot be reached, 401 when the token is not good)."""
    verifier: Verifier = request.app.state.verifier
    try:
        return await verifier.verify(token)
    except OidcUnavailable as e:
        log.warning("central account unavailable: %s", e)
        raise ApiError("account_unavailable", "the central account is unavailable; try again later", 503) from None
    except OidcError as e:
        log.info("token rejected: %s", e)
        raise _unauthorized() from None


async def _device_caller(request: Request, token: str) -> Caller:
    tokens: AccountTokens | None = request.app.state.account_tokens
    if tokens is None:
        raise _unauthorized()
    try:
        claims = tokens.verify(token)
    except TokenError as e:
        log.info("account token rejected: %s", e)
        raise _unauthorized() from None
    accounts: AccountStore = request.app.state.accounts
    if await accounts.device(claims.account, claims.device) is None:
        log.info("account token of a revoked device rejected")
        raise _unauthorized()
    await accounts.touch(claims.account)
    await accounts.touch_device(claims.device)
    config: CloudConfig = request.app.state.config
    return Caller(
        key=claims.account,
        client_id=platform_client(claims.platform, config.clients),
        platform=claims.platform,
        device=claims.device,
    )


async def current_account(request: Request, authorization: str | None = Header(default=None)) -> Caller:
    """Dependency for the routes of 0.3.x: an account token, or a Zitadel token while the transition flag is on."""
    token = _bearer(authorization)
    if AccountTokens.looks_like(token):
        # Never handed to the Zitadel verifier, whatever happens to it here.
        return await _device_caller(request, token)
    config: CloudConfig = request.app.state.config
    if not config.legacy_zitadel_tokens:
        raise _unauthorized("sign in with an account session")
    identity = await zitadel_identity(request, token)
    accounts: AccountStore = request.app.state.accounts
    # The id 0.3.x gave the account ("<issuer>#<subject>"), so old app builds keep their agenda.
    key = await accounts.resolve_zitadel(identity.issuer, identity.subject, legacy_id=True)
    return Caller(
        key=key,
        client_id=identity.client_id,
        platform=client_platform(identity.client_id, config.clients),
        device=None,
    )


async def current_device(request: Request, authorization: str | None = Header(default=None)) -> Caller:
    """Dependency for the routes that came with account sessions: an account token only."""
    if request.app.state.account_tokens is None:
        raise ApiError("not_configured", "this cloud does not issue account sessions", 404)
    token = _bearer(authorization)
    if not AccountTokens.looks_like(token):
        raise _unauthorized("sign in with an account session")
    return await _device_caller(request, token)


def require_platform(caller: Caller, allowed: frozenset[str]) -> None:
    if caller.platform not in allowed:
        raise ApiError("forbidden", "this device cannot do this", 403)
