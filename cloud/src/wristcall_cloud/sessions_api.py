"""Account sessions: silent accounts made from an account key, devices enrolled by that key or by a fresh Zitadel
token, rotating refresh tokens, the account's devices and its key.

Every route here needs the signing key (it signs the access tokens): without one they answer 404 `not_configured`
first. Secrets (account key, refresh tokens, Zitadel and access tokens) never reach a log or an error message.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response

from .account_store import AccountStore, DeviceLimit, KeyExists, KeyInUse, NoKey, RefreshReused, WrongKey
from .api_common import ApiError, json_object, parse_device
from .auth import Caller, _bearer, client_platform, current_device, require_platform, zitadel_identity
from .config import CloudConfig
from .limits import check_ip_limit
from .session_tokens import APP_PLATFORMS, MANAGER_PLATFORMS, AccountTokens, parse_account_key

log = logging.getLogger("wristcall_cloud.sessions")

router = APIRouter()

MAX_REFRESH_TOKEN = 128
CAP_RETRY_AFTER_S = 3600


def _require_signing_key(request: Request) -> AccountTokens:
    # Imported here: app.py imports this module to include its router.
    from .app import signing_key

    signing_key(request)
    return request.app.state.account_tokens


def _accounts(request: Request) -> AccountStore:
    return request.app.state.accounts


def _config(request: Request) -> CloudConfig:
    return request.app.state.config


def _key_hash(value: Any, field: str = "account_key") -> str:
    try:
        return parse_account_key(value)
    except ValueError:
        raise ApiError("invalid", f"{field} must be 32 bytes in base64url", 422) from None


def _unknown_key() -> ApiError:
    return ApiError("unauthorized", "unknown account key", 401)


def _device_limit() -> ApiError:
    return ApiError("limit", "this account has reached its device limit", 403)


def _key_in_use() -> ApiError:
    return ApiError("key_in_use", "this account key belongs to another account", 409)


def session_response(
    account: str, device_doc: dict[str, Any], refresh_token: str, tokens: AccountTokens
) -> dict[str, Any]:
    """A device session: a new access token for the device and the refresh token given."""
    access_token, expires_at = tokens.issue(account, device_doc["_id"], device_doc["platform"])
    return {
        "account": account,
        "device": device_doc["_id"],
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }


@router.post("/v1/accounts", status_code=201)
async def create_account(request: Request) -> dict[str, Any]:
    tokens = _require_signing_key(request)
    check_ip_limit(request, request.app.state.account_limiter)
    body = await json_object(request)
    key_hash = _key_hash(body.get("account_key"))
    device = parse_device(body.get("device"))
    config = _config(request)
    accounts = _accounts(request)
    created = await accounts.count_created_since(time.time() - 86400)
    if created >= config.accounts_per_day:
        log.warning("account creation cap reached")
        raise ApiError("rate_limited", "too many requests; try again later", 429, retry_after=CAP_RETRY_AFTER_S)
    try:
        account = await accounts.create_account(key_hash)
    except KeyInUse:
        raise _key_in_use() from None
    if created + 1 > config.accounts_alert_per_day:
        log.warning("accounts created in 24 h: %d", created + 1)
    # A brand-new account has no device: the limit cannot be reached here.
    doc, refresh = await accounts.add_device(account["_id"], device, max_devices=config.max_devices)
    log.info("account created (platform %s)", device["platform"])
    return session_response(account["_id"], doc, refresh, tokens)


def _platforms_of_client(client_id: str | None, config: CloudConfig) -> frozenset[str]:
    """The device platforms a Zitadel token's app may enroll: the iOS app any Apple app platform but the watch's,
    the watch app the watch, the PWA the web."""
    platform = client_platform(client_id, config.clients)
    return APP_PLATFORMS if platform in APP_PLATFORMS else frozenset({platform})


@router.post("/v1/devices", status_code=201)
async def create_device(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    tokens = _require_signing_key(request)
    check_ip_limit(request, request.app.state.attempt_limiter)
    body = await json_object(request)
    if authorization is not None and "account_key" in body:
        raise ApiError("invalid", "send either an account key or a sign-in token", 422)
    config = _config(request)
    accounts = _accounts(request)
    if authorization is None:
        key_hash = _key_hash(body.get("account_key"))
        device = parse_device(body.get("device"))
        doc = await accounts.account_by_key(key_hash)
        claimed = body.get("account")
        if doc is None or (claimed is not None and claimed != doc["_id"]):
            log.info("device enrollment with an unknown account key")
            raise _unknown_key()
        account, key_set = doc["_id"], True
    else:
        token = _bearer(authorization)
        device = parse_device(body.get("device"))
        if AccountTokens.looks_like(token):
            # An account session is not a sign-in token; never handed to the Zitadel verifier.
            raise ApiError("unauthorized", "sign in again to add a device", 401)
        identity = await zitadel_identity(request, token)
        if time.time() - identity.issued_at > config.zitadel_max_age_s:
            raise ApiError(
                "stale_token",
                f"sign in again: the sign-in token is older than {config.zitadel_max_age_s} seconds",
                401,
            )
        if device["platform"] not in _platforms_of_client(identity.client_id, config):
            raise ApiError("forbidden", "this app cannot add a device of that platform", 403)
        account = await accounts.resolve_zitadel(identity.issuer, identity.subject, legacy_id=False)
        doc = await accounts.get_account(account)
        key_set = doc is not None and "key_hash" in doc
    try:
        device_doc, refresh = await accounts.add_device(account, device, max_devices=config.max_devices)
    except DeviceLimit:
        raise _device_limit() from None
    await accounts.touch(account)
    log.info("device added (platform %s)", device["platform"])
    return {**session_response(account, device_doc, refresh, tokens), "key_set": key_set}


@router.post("/v1/session/refresh")
async def refresh_session(request: Request) -> dict[str, Any]:
    tokens = _require_signing_key(request)
    body = await json_object(request)
    refresh_token = body.get("refresh_token")
    # `assertion` (App Attest) is accepted and ignored in phase A.
    if (
        not isinstance(refresh_token, str)
        or not refresh_token.startswith("wcr_")
        or len(refresh_token) > MAX_REFRESH_TOKEN
    ):
        raise ApiError("unauthorized", "unknown or expired refresh token", 401)
    config = _config(request)
    accounts = _accounts(request)
    try:
        rotated = await accounts.rotate_refresh(refresh_token, idle_s=config.account_idle_days * 86400)
    except RefreshReused:
        log.warning("refresh token reused; device revoked")
        raise ApiError("revoked", "this device was signed out; sign in again", 401) from None
    if rotated is None:
        raise ApiError("unauthorized", "unknown or expired refresh token", 401)
    device_doc, new_refresh = rotated
    await accounts.touch(device_doc["account"])
    return session_response(device_doc["account"], device_doc, new_refresh, tokens)


def _public_device(doc: dict[str, Any], current: str | None) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "name": doc["name"],
        "platform": doc["platform"],
        "created_at": doc["created_at"],
        "last_seen_at": doc.get("last_seen_at"),
        "current": doc["_id"] == current,
    }


@router.get("/v1/devices")
async def list_devices(request: Request, caller: Caller = Depends(current_device)) -> dict[str, Any]:
    docs = await _accounts(request).list_devices(caller.key)
    return {"devices": [_public_device(doc, caller.device) for doc in docs]}


@router.delete("/v1/devices/{device_id}", status_code=204)
async def delete_device(device_id: str, request: Request, caller: Caller = Depends(current_device)) -> Response:
    # Any device may sign itself out; only the apps and the web app sign out the others.
    if device_id != caller.device:
        require_platform(caller, MANAGER_PLATFORMS)
    if not await _accounts(request).delete_device(caller.key, device_id):
        raise ApiError("not_found", "device not found", 404)
    log.info("device removed")
    return Response(status_code=204)


@router.put("/v1/account/key", status_code=204)
async def put_account_key(request: Request, caller: Caller = Depends(current_device)) -> Response:
    require_platform(caller, APP_PLATFORMS)
    body = await json_object(request)
    key_hash = _key_hash(body.get("account_key"))
    try:
        await _accounts(request).set_key(caller.key, key_hash)
    except KeyExists:
        raise ApiError("key_exists", "this account already has an account key; rotate it instead", 409) from None
    except KeyInUse:
        raise _key_in_use() from None
    return Response(status_code=204)


@router.post("/v1/account/key/rotate")
async def rotate_account_key(request: Request, caller: Caller = Depends(current_device)) -> dict[str, Any]:
    require_platform(caller, APP_PLATFORMS)
    check_ip_limit(request, request.app.state.attempt_limiter)
    body = await json_object(request)
    current_hash = _key_hash(body.get("account_key"))
    new_hash = _key_hash(body.get("new_account_key"), "new_account_key")
    accounts = _accounts(request)
    try:
        # current_device takes account tokens only, so the caller always has a device.
        revoked = await accounts.rotate_key(caller.key, current_hash, new_hash, keep_device=caller.device or "")
    except WrongKey:
        raise ApiError("unauthorized", "account_key does not match", 401) from None
    except NoKey:
        raise ApiError("no_key", "this account has no account key; set one first", 409) from None
    except KeyInUse:
        raise _key_in_use() from None
    log.info("account key rotated (%d devices signed out)", revoked)
    return {"revoked": revoked}
