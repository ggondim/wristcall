"""Account sessions: silent accounts made from an account key, devices enrolled by that key or by a fresh
Zitadel token, rotating refresh tokens, the account's devices and its key; enroll codes (an app adds a watch),
web sessions approved by an app, and beta invites.

Every route here needs the signing key (it signs the access tokens): without one they answer 404 `not_configured`
first. Secrets (account key, refresh tokens, enroll codes, web session keys, invite codes,
Zitadel and access tokens) never reach a log or an error message.
"""

import logging
import re
import time
import unicodedata
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response

from .account_store import (
    AccountStore,
    AlreadyEntitled,
    CodeLimit,
    DeviceLimit,
    KeyExists,
    KeyInUse,
    NoKey,
    RefreshReused,
    WrongKey,
)
from .api_common import ApiError, json_object, parse_device
from .auth import Caller, _bearer, client_platform, current_device, require_platform, zitadel_identity
from .config import CloudConfig
from .limits import check_ip_limit
from .session_tokens import APP_PLATFORMS, MANAGER_PLATFORMS, AccountTokens, parse_account_key

log = logging.getLogger("wristcall_cloud.sessions")

router = APIRouter()

MAX_REFRESH_TOKEN = 128
MAX_CODE = 64  # enroll codes and invites; longer is never a code
MAX_WEB_KEY = 128
MAX_USER_AGENT = 200
_SESSION_ID = re.compile(r"ws_[0-9a-f]{16}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_NO_COUNTRY = frozenset({"XX", "T1"})  # Cloudflare: unknown, Tor
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


def _stale_token(config: CloudConfig) -> ApiError:
    return ApiError(
        "stale_token", f"sign in again: the sign-in token is older than {config.zitadel_max_age_s} seconds", 401
    )


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
            raise _stale_token(config)
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


async def _check_reset_sign_in(request: Request, token: Any, caller: Caller) -> None:
    """A key reset needs a fresh sign-in of the iOS app by a user of the caller's own account."""
    if not isinstance(token, str) or not token:
        raise ApiError("invalid", "zitadel_token must be a non-empty string", 422)
    if AccountTokens.looks_like(token):
        # An account session is not a sign-in token; never handed to the Zitadel verifier.
        raise ApiError("unauthorized", "sign in again to reset the account key", 401)
    identity = await zitadel_identity(request, token)
    config = _config(request)
    if time.time() - identity.issued_at > config.zitadel_max_age_s:
        raise _stale_token(config)
    if client_platform(identity.client_id, config.clients) not in APP_PLATFORMS:
        raise ApiError("forbidden", "this app cannot reset the account key", 403)
    if await _accounts(request).account_for_identity("zitadel", identity.issuer, identity.subject) != caller.key:
        raise ApiError("forbidden", "this sign-in belongs to another account", 403)


@router.post("/v1/account/key/rotate")
async def rotate_account_key(request: Request, caller: Caller = Depends(current_device)) -> dict[str, Any]:
    """Swaps the account key, proven by the current key (`account_key`) or by a fresh sign-in (`zitadel_token`);
    the sign-in sets the key whether or not one exists, so a lost or hijacked key can be taken back."""
    require_platform(caller, APP_PLATFORMS)
    check_ip_limit(request, request.app.state.attempt_limiter)
    body = await json_object(request)
    by_key, by_sign_in = body.get("account_key") is not None, body.get("zitadel_token") is not None
    if by_key == by_sign_in:
        raise ApiError("invalid", "send either account_key or zitadel_token", 422)
    current_hash = _key_hash(body.get("account_key")) if by_key else None
    new_hash = _key_hash(body.get("new_account_key"), "new_account_key")
    accounts = _accounts(request)
    # current_device takes account tokens only, so the caller always has a device.
    keep_device = caller.device or ""
    try:
        if current_hash is not None:
            revoked = await accounts.rotate_key(caller.key, current_hash, new_hash, keep_device=keep_device)
        else:
            await _check_reset_sign_in(request, body.get("zitadel_token"), caller)
            revoked = await accounts.reset_key(caller.key, new_hash, keep_device=keep_device)
    except WrongKey:
        raise ApiError("unauthorized", "account_key does not match", 401) from None
    except NoKey:
        raise ApiError("no_key", "this account has no account key; set one first", 409) from None
    except KeyInUse:
        raise _key_in_use() from None
    if current_hash is not None:
        log.info("account key rotated (%d devices signed out)", revoked)
    else:
        log.info("account key reset by a fresh sign-in (%d devices signed out)", revoked)
    return {"revoked": revoked}


def entitlement_response(doc: dict[str, Any]) -> dict[str, Any]:
    """An entitlement as the API shows it (`GET /v1/account` too)."""
    return {"plan": doc.get("plan"), "source": doc.get("source"), "expires_at": doc.get("expires_at")}


def _code(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError("invalid", "code must be a non-empty string", 422)
    return value


def _invalid_code() -> ApiError:
    return ApiError("invalid_code", "unknown, used or expired code", 404)


def _web_session_not_found() -> ApiError:
    return ApiError("not_found", "web session not found", 404)


# Enroll codes: an app hands a code to the watch (WatchConnectivity), which enrolls itself with it


@router.post("/v1/devices/enroll-codes", status_code=201)
async def create_enroll_code(request: Request, caller: Caller = Depends(current_device)) -> dict[str, Any]:
    require_platform(caller, APP_PLATFORMS)
    config = _config(request)
    try:
        code, expires_at = await _accounts(request).add_enroll_code(caller.key, max_open=config.max_enroll_codes)
    except CodeLimit:
        raise ApiError("limit", f"this account has {config.max_enroll_codes} open enroll codes", 403) from None
    log.info("enroll code created")
    return {"code": code, "expires_at": expires_at}


@router.post("/v1/devices/enroll", status_code=201)
async def enroll_with_code(request: Request) -> dict[str, Any]:
    tokens = _require_signing_key(request)
    check_ip_limit(request, request.app.state.attempt_limiter)
    body = await json_object(request)
    code = _code(body.get("code"))
    device = parse_device(body.get("device"))
    accounts = _accounts(request)
    doc = None if len(code) > MAX_CODE else await accounts.enroll_code(code)
    if doc is None:
        log.info("enrollment with an unknown enroll code")
        raise _invalid_code()
    # Checked before spending: a code meant for the watch never makes an app device, and stays usable.
    if device["platform"] not in doc["platforms"]:
        raise ApiError("forbidden", "this code cannot add a device of that platform", 403)
    account = await accounts.take_enroll_code(code, device["platform"])
    if account is None:  # spent or expired in the meantime
        raise _invalid_code()
    try:
        device_doc, refresh = await accounts.add_device(account, device, max_devices=_config(request).max_devices)
    except DeviceLimit:
        # The code is spent already (accepted: the app makes another one once a device is removed).
        raise _device_limit() from None
    await accounts.touch(account)
    log.info("device enrolled by code (platform %s)", device["platform"])
    return session_response(account, device_doc, refresh, tokens)


# Web sessions: the web app shows a QR (session id and approve key), an app approves it, the web app polls with the
# poll key (which never leaves it) and gets its session once


def _user_agent(request: Request) -> str:
    value = request.headers.get("user-agent", "")
    return "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")[:MAX_USER_AGENT]


def _country(request: Request, header: str | None) -> str | None:
    if not header:
        return None
    value = request.headers.get(header, "").strip()
    return value if _COUNTRY.fullmatch(value) and value not in _NO_COUNTRY else None


@router.post("/v1/web-sessions", status_code=201)
async def create_web_session(request: Request) -> dict[str, Any]:
    _require_signing_key(request)
    check_ip_limit(request, request.app.state.attempt_limiter)
    body = await json_object(request)
    raw = body.get("device")
    if isinstance(raw, dict) and "platform" not in raw:
        raw = {**raw, "platform": "web"}
    device = parse_device(raw)
    if device["platform"] != "web":
        raise ApiError("invalid", "device.platform must be web", 422)
    country = _country(request, _config(request).client_country_header)
    session = await _accounts(request).add_web_session(device, _user_agent(request), country)
    log.info("web session created")
    return session


@router.get("/v1/web-sessions/{session_id}")
async def poll_web_session(
    session_id: str, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    tokens = _require_signing_key(request)
    poll_key = _bearer(authorization)
    if not _SESSION_ID.fullmatch(session_id):
        raise _web_session_not_found()
    # No attempt limit (the poll key has 256 bits); one poll a second per session.
    if request.app.state.poll_limiter.allow(session_id) is not None:
        raise ApiError("rate_limited", "poll at most once a second", 429, retry_after=1)
    accounts = _accounts(request)
    doc = None if len(poll_key) > MAX_WEB_KEY else await accounts.take_web_session(session_id, poll_key)
    if doc is None:
        raise _web_session_not_found()
    if doc["status"] == "pending":
        return {"status": "pending", "expires_at": doc["expires_at"]}
    # The approved session was taken (deleted) above, so at the device limit (a device added since the approval,
    # which checked it) it is gone and the web app starts over (accepted: nothing secret was issued).
    try:
        device_doc, refresh = await accounts.add_device(
            doc["account"], doc["device"], max_devices=_config(request).max_devices
        )
    except DeviceLimit:
        raise _device_limit() from None
    log.info("web session signed in")
    return {"status": "approved", **session_response(doc["account"], device_doc, refresh, tokens)}


async def _pending_web_session(request: Request, session_id: str, caller: Caller) -> tuple[dict[str, Any], str]:
    """The pending session the body's approve key opens (404 otherwise), for an app."""
    require_platform(caller, APP_PLATFORMS)
    body = await json_object(request)
    approve_key = body.get("approve_key")
    if not isinstance(approve_key, str) or not approve_key:
        raise ApiError("invalid", "approve_key must be a non-empty string", 422)
    if not _SESSION_ID.fullmatch(session_id) or len(approve_key) > MAX_WEB_KEY:
        raise _web_session_not_found()
    doc = await _accounts(request).web_session(session_id, approve_key)
    if doc is None:
        raise _web_session_not_found()
    return doc, approve_key


@router.post("/v1/web-sessions/{session_id}/inspect")
async def inspect_web_session(
    session_id: str, request: Request, caller: Caller = Depends(current_device)
) -> dict[str, Any]:
    doc, _ = await _pending_web_session(request, session_id, caller)
    return {
        "name": doc["device"]["name"],
        "user_agent": doc["user_agent"],
        "country": doc["country"],
        "created_at": doc["created_at"],
        "expires_at": doc["expires_at"],
    }


@router.post("/v1/web-sessions/{session_id}/approve")
async def approve_web_session(
    session_id: str, request: Request, caller: Caller = Depends(current_device)
) -> dict[str, Any]:
    _, approve_key = await _pending_web_session(request, session_id, caller)
    accounts = _accounts(request)
    if await accounts.count_devices(caller.key) >= _config(request).max_devices:
        raise _device_limit()
    if not await accounts.approve_web_session(session_id, approve_key, caller.key):
        raise _web_session_not_found()
    log.info("web session approved")
    return {"approved": True}


# Beta invites


@router.post("/v1/entitlements/redeem")
async def redeem_invite(request: Request, caller: Caller = Depends(current_device)) -> dict[str, Any]:
    require_platform(caller, MANAGER_PLATFORMS)
    check_ip_limit(request, request.app.state.attempt_limiter)
    body = await json_object(request)
    code = _code(body.get("code"))
    try:
        doc = None if len(code) > MAX_CODE else await _accounts(request).redeem_invite(code, caller.key)
    except AlreadyEntitled:
        raise ApiError("already_entitled", "this account already has a plan", 409) from None
    if doc is None:
        log.info("redeem with an unknown invite")
        raise _invalid_code()
    log.info("invite redeemed (plan %s)", doc["plan"])
    return entitlement_response(doc)
