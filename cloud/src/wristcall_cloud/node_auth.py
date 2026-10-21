"""Who is calling a node route (`/v1/node/*`): the node, the user behind it, and the call it works on.

A node sends three credentials, each one checked here and nowhere else:
- its own secret (`Authorization: Bearer wc_node_...`): `current_node`, with a rate limit per node;
- the per-server token of the user (`X-Wristcall-User-Token`, `wc-server+jwt`), which the app got from the Cloud
  with the node's URL as `aud` and the device and platform as `did` and `plt`: `node_caller`;
- the call grant (`X-Wristcall-Call-Grant`, `wc-grant+jwt`), which the Cloud issued when the call opened and which
  only the node of that call may present: `verify_grant`.
The three JWT types share the Cloud's key and are told apart by the header `typ`. Nothing here puts a credential in a
log or a message.
"""

import logging
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, Header, Request

from .account_store import AccountStore
from .api_common import ApiError
from .config import CloudConfig
from .nodes import NodeStore, is_node_secret
from .push.limits import _Limiter, _Window
from .session_tokens import MANAGER_PLATFORMS, MAX_TOKEN_LENGTH, PLATFORMS
from .signing import ALGORITHM, TOKEN_TYPE, SigningKey

log = logging.getLogger("wristcall_cloud.node_auth")

GRANT_TYPE = "wc-grant+jwt"
GRANT_SLACK_S = 3600  # a grant outlives the longest call by this much: the node's queue may deliver `finish` late
EXPIRED_GRANT_WINDOW_S = 86400  # an expired grant still closes its call (`finish`) for this long
LEEWAY_S = 60  # clock difference tolerated between the Cloud and what it signed, on the user token


class NodeLimiter(_Limiter):
    """Requests per node (by its id) on the node routes."""

    def __init__(self, per_minute: int, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__([_Window(60, per_minute)], now)


@dataclass(frozen=True)
class NodeCaller:
    account: str
    device: str
    platform: str
    node: dict[str, Any]


@dataclass(frozen=True)
class CallGrant:
    account: str
    device: str
    platform: str
    call_id: str
    node_id: str
    expired: bool


def _unauthorized(message: str = "missing or invalid credential") -> ApiError:
    return ApiError("unauthorized", message, 401)


def _signer(request: Request) -> tuple[SigningKey, str]:
    key: SigningKey | None = request.app.state.signing_key
    config: CloudConfig = request.app.state.config
    if key is None or not config.public_url:
        raise ApiError("not_configured", "this cloud has no nodes", 404)
    return key, config.public_url


async def current_node(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """The enabled node whose secret the request carries (401 otherwise; 429 above its rate limit)."""
    scheme, _, secret = (authorization or "").partition(" ")
    secret = secret.strip()
    # The shape first: a string that cannot be a secret costs no query.
    if scheme.lower() != "bearer" or not is_node_secret(secret):
        raise _unauthorized("missing or invalid node credential")
    node = await NodeStore(request.app.state.cloud).by_secret(secret)
    if node is None:
        log.info("node credential rejected")
        raise _unauthorized("missing or invalid node credential")
    limiter: NodeLimiter = request.app.state.node_limiter
    wait = limiter.allow(node["_id"])
    if wait is not None:
        raise ApiError("rate_limited", "too many requests; try again later", 429, retry_after=math.ceil(wait))
    return node


def _claims(
    token: Any, key: SigningKey, *, typ: str, issuer: str, audience: str, required: list[str]
) -> dict[str, Any]:
    """The verified claims of a JWT of type `typ` (signature, `iss`, `aud`, required claims, numeric `exp` and
    `iat`); the time claims are left to the caller. 401 `unauthorized` for anything else."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
        raise _unauthorized()
    try:
        header = jwt.get_unverified_header(token)
        if header.get("typ") != typ or header.get("alg") != ALGORITHM:
            raise _unauthorized()
        claims = jwt.decode(
            token,
            key.public_key,
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=issuer,
            # The time claims are checked by the caller against the wall clock.
            options={"require": ["exp", "iat", "iss", "aud", *required], "verify_exp": False, "verify_iat": False},
        )
    except jwt.PyJWTError:
        raise _unauthorized() from None
    exp, iat = claims.get("exp"), claims.get("iat")
    for value in (exp, iat):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _unauthorized()
    if claims.get("aud") != audience:  # a list of audiences would carry the token to more than one place
        raise _unauthorized()
    if iat - LEEWAY_S > time.time():
        raise _unauthorized()
    return claims


def _identity(claims: dict[str, Any]) -> tuple[str, str, str]:
    account, device, platform = claims.get("sub"), claims.get("did"), claims.get("plt")
    if not all(isinstance(v, str) and v for v in (account, device)) or platform not in PLATFORMS:
        raise _unauthorized()
    return account, device, platform


async def node_caller(
    request: Request,
    node: dict[str, Any] = Depends(current_node),
    x_wristcall_user_token: str | None = Header(default=None),
) -> NodeCaller:
    """The user the node acts for: its per-server token must name this node (`aud`) and carry a device and platform.
    401 for a token that is not good, 403 `device_revoked` for a device the account no longer has."""
    key, issuer = _signer(request)
    claims = _claims(
        x_wristcall_user_token, key, typ=TOKEN_TYPE, issuer=issuer, audience=node["url"], required=["sub", "did", "plt"]
    )
    if claims["exp"] + LEEWAY_S < time.time():
        raise _unauthorized("user token expired")
    account, device, platform = _identity(claims)
    accounts: AccountStore = request.app.state.accounts
    if await accounts.device(account, device) is None:
        log.info("user token of a revoked device rejected")
        raise ApiError("device_revoked", "this device was removed from the account", 403)
    return NodeCaller(account=account, device=device, platform=platform, node=node)


def require_manager(caller: NodeCaller) -> None:
    """The routes that change agents are for the apps that manage the account, not the watch."""
    if caller.platform not in MANAGER_PLATFORMS:
        raise ApiError("forbidden", "this device cannot do this", 403)


def issue_grant(key: SigningKey, *, issuer: str, caller: NodeCaller, call_id: str, max_s: int, now: float) -> str:
    """The grant for one call on one node: valid for the call's longest length plus `GRANT_SLACK_S`."""
    issued = int(now)
    claims = {
        "iss": issuer,
        "aud": issuer,
        "sub": caller.account,
        "did": caller.device,
        "plt": caller.platform,
        "call": call_id,
        "node": caller.node["_id"],
        "iat": issued,
        "exp": issued + max_s + GRANT_SLACK_S,
        "jti": secrets.token_hex(16),
    }
    return key.sign(claims, typ=GRANT_TYPE)


def verify_grant(
    request: Request, node: dict[str, Any], call_id: str, token: str | None, *, allow_expired: bool
) -> CallGrant:
    """The grant of `call_id` that `node` presents. Only the node it was issued to, for the call it names. One that
    expired is refused, except with `allow_expired` (`finish`) within `EXPIRED_GRANT_WINDOW_S` (then `expired`)."""
    key, issuer = _signer(request)
    claims = _claims(
        token, key, typ=GRANT_TYPE, issuer=issuer, audience=issuer, required=["sub", "did", "plt", "call", "node"]
    )
    account, device, platform = _identity(claims)
    if claims["node"] != node["_id"] or claims["call"] != call_id:
        raise _unauthorized()
    now = time.time()
    expired = claims["exp"] < now
    if expired and (not allow_expired or claims["exp"] + EXPIRED_GRANT_WINDOW_S < now):
        raise _unauthorized("call grant expired")
    return CallGrant(
        account=account, device=device, platform=platform, call_id=call_id, node_id=node["_id"], expired=expired
    )
