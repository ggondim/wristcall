"""HTTP API of the cloud. Callers authenticate with an account session token (`Authorization: Bearer`, see auth.py),
or with a Zitadel token on the routes of 0.3.x while the transition flag is on; except on the push relay
(push/api.py), whose keys are their own credentials.

Every error is `{"error": code, "message": ...}`. Request bodies are capped at MAX_BODY bytes. Rate limiting is
left to the reverse proxy of the deployment (see README), except for the push relay's own limits (push/limits.py)
and those of the account routes (limits.py, sessions_api.py).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pymongo.errors import DuplicateKeyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .account_store import AccountStore, purge_idle_accounts_loop
from .agenda import AgendaError, parse_agents, parse_new_server, parse_server_patch
from .api_common import ApiError, json_object
from .audience import AudienceError, is_loopback, normalize_audience
from .auth import Caller, Verifier, current_account, require_platform
from .cloud_store import CloudModeStore
from .config import (
    CloudConfig,
    ConfigError,
    apns_provider_token,
    check_public_url,
    check_push_fake,
    same_url,
    vapid_identity,
)
from .identity import IdentityUnavailable, ZitadelUsers
from .limits import AccountCreationLimiter, AttemptLimiter, PollLimiter
from .node_auth import NodeLimiter
from .node_agents import router as node_agents_router
from .node_calls import router as node_calls_router
from .node_me import router as node_me_router
from .nodes import NodeStore
from .oidc import OidcVerifier
from .push.api import push_error_response
from .push.api import router as push_router
from .push.apns import ApnsChannel
from .push.channels import Channel, FakeChannel
from .push.limits import RegistrationLimiter, SendLimiter
from .push.registry import PushError, purge_idle_loop
from .push.webpush import WebPushChannel
from .session_tokens import MANAGER_PLATFORMS, AccountTokens
from .sessions_api import entitlement_response
from .sessions_api import router as sessions_router
from .signing import SigningKey, server_token_claims
from .store import Store, open_store, public_server

log = logging.getLogger("wristcall_cloud.app")

MAX_BODY = 64 * 1024
NODE_MAX_BODY = 1024 * 1024  # on /v1/node/* only: envelopes and history entries are bigger than account requests
NODE_PREFIX = "/v1/node/"
BASE_SCOPES = ["openid", "profile", "offline_access"]


class Users(Protocol):
    async def delete(self, user_id: str) -> bool: ...


def _error(code: str, message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)


class BodyTooLarge(HTTPException):
    """An HTTPException on purpose: FastAPI re-raises those while reading a model body, but turns anything else
    into 400."""

    def __init__(self, limit: int = MAX_BODY) -> None:
        super().__init__(status_code=413, detail=f"request body is limited to {limit} bytes")


class BodyLimit:
    """Rejects bodies over `limit` bytes (NODE_MAX_BODY on the node API): up front by Content-Length, otherwise
    while the body is read."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = NODE_MAX_BODY if scope["path"].startswith(NODE_PREFIX) else self.limit
        for name, value in scope["headers"]:
            if name == b"content-length" and (not value.isdigit() or int(value) > limit):
                await self._reject(scope, receive, send, limit)
                return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if started:
                raise
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        await _error("too_large", BodyTooLarge(limit).detail, 413)(scope, receive, send)


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_accounts(request: Request) -> AccountStore:
    return request.app.state.accounts


def get_cloud(request: Request) -> CloudModeStore:
    return request.app.state.cloud


def public_config(
    config: CloudConfig,
    *,
    server_tokens: bool = False,
    channels: dict[str, Channel] | None = None,
    vapid_public_key: str | None = None,
) -> dict[str, Any]:
    """`server_tokens`: the Cloud has a signing key, which also signs the account session tokens."""
    scopes = list(BASE_SCOPES)
    if config.project_id:
        scopes.append(f"urn:zitadel:iam:org:project:id:{config.project_id}:aud")
    if config.zitadel_org_id:
        # The login shows the org's branding and texts only when the request names the org (project private
        # labeling does not reach the hosted login v2, zitadel#10692).
        scopes.append(f"urn:zitadel:iam:org:id:{config.zitadel_org_id}")
    return {
        "issuer": config.issuer,
        "project_id": config.project_id,
        "clients": dict(config.clients),
        "scopes": scopes,
        "server_tokens": server_tokens,
        "push": {
            "apns": "apns" in (channels or {}),
            "webpush": "webpush" in (channels or {}),
            "vapid_public_key": vapid_public_key if "webpush" in (channels or {}) else None,
            "apns_topics": list(config.apns_topics) if "apns" in (channels or {}) else [],
        },
        "sessions": {
            "enabled": server_tokens,
            "zitadel_max_age_s": config.zitadel_max_age_s,
            "legacy_zitadel_tokens": config.legacy_zitadel_tokens,
        },
    }


def signing_key(request: Request) -> SigningKey:
    """The key per-server tokens are signed with; 404 `not_configured` before anything else when there is none."""
    key: SigningKey | None = request.app.state.signing_key
    if key is None:
        raise ApiError("not_configured", "this cloud does not issue server tokens", 404)
    return key


def parse_audience(body: dict[str, Any], config: CloudConfig) -> str:
    raw = body.get("audience")
    if not isinstance(raw, str):
        raise ApiError("invalid", "audience must be a string", 422)
    try:
        audience = normalize_audience(raw)
    except AudienceError as e:
        raise ApiError("invalid", str(e), 422) from None
    if is_loopback(audience) and not config.allow_loopback_audience:
        raise ApiError("invalid", "audience must not be a loopback address", 422)
    # A token for the Cloud or for the issuer would be a credential where none is expected: refuse it outright.
    if any(same_url(audience, own) for own in (config.public_url, config.issuer) if own):
        raise ApiError("invalid", "audience must be a server, not the cloud or the issuer", 422)
    return audience


router = APIRouter()
_STATUS = {"invalid": 422, "limit": 403}


def _not_found() -> ApiError:
    return ApiError("not_found", "server not found", 404)


@router.get("/v1/account")
async def get_account(request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
    store = get_store(request)
    account = await store.touch_account(caller.key)
    identities = await get_accounts(request).identities_of(caller.key)
    return {
        "account": caller.key,
        "created_at": account["created_at"],
        "servers": await store.count_servers(caller.key),
        "plan": await _plan(get_accounts(request), caller.key),
        "identities": [identity["kind"] for identity in identities],
        "key_set": "key_hash" in account,
    }


async def _plan(accounts: AccountStore, account: str) -> dict[str, Any] | None:
    """The account's entitlement while it has not expired."""
    doc = await accounts.entitlement(account)
    return None if doc is None else entitlement_response(doc)


@router.delete("/v1/account", status_code=204)
async def delete_account(request: Request, caller: Caller = Depends(current_account)) -> Response:
    # Only the apps (and the web app) may delete the account; the watch (or a token of an unknown client) may not.
    require_platform(caller, MANAGER_PLATFORMS)
    config: CloudConfig = request.app.state.config
    accounts = get_accounts(request)
    # The agenda first, but not the account document: until the end the account, its devices and its identities
    # stay, so the same token retries after a failure below (an empty agenda deletes again without harm).
    await get_store(request).servers.delete_many({"account": caller.key})
    # Then the users at the issuer (decision 23). If that fails nothing else is deleted: the app shows the error
    # and retries.
    users: Users | None = request.app.state.users
    if users is not None:
        for identity in await accounts.identities_of(caller.key):
            if identity["kind"] != "zitadel" or identity["issuer"] != config.issuer:
                continue
            try:
                deleted = await users.delete(identity["subject"])
            except IdentityUnavailable as e:
                log.warning("account agenda deleted, its sign-in not: %s", e)
                raise ApiError(
                    "account_unavailable", "the sign-in could not be deleted; try again later", 503
                ) from None
            if not deleted:
                # Expected for a user of another organization; for every user it means a wrong
                # WRISTCALL_CLOUD_ZITADEL_ORG_ID.
                log.warning("account deleted; its sign-in is not a user of the Cloud's organization (left alone)")
    # Devices, identities, codes, web sessions and entitlement; the account document last.
    await accounts.delete_account_data(caller.key)
    return Response(status_code=204)


@router.get("/v1/servers")
async def list_servers(request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
    return {"servers": [public_server(s) for s in await get_store(request).list_servers(caller.key)]}


@router.post("/v1/servers", status_code=201)
async def create_server(request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
    fields = parse_new_server(await json_object(request))
    store = get_store(request)
    await store.touch_account(caller.key)
    # Check then insert: two simultaneous requests may overshoot the limit by one server. Accepted, the limit
    # protects the database from runaway clients, not a quota. The unique index is what decides duplicates.
    if await store.count_servers(caller.key) >= request.app.state.config.max_servers:
        raise ApiError("limit", "the account reached its limit of servers", 403)
    try:
        doc = await store.add_server(caller.key, fields)
    except DuplicateKeyError:
        raise ApiError("conflict", "the account already has a server with this url", 409) from None
    return public_server(doc)


@router.get("/v1/servers/{server_id}")
async def get_server(server_id: str, request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
    doc = await get_store(request).get_server(caller.key, server_id)
    if doc is None:
        raise _not_found()
    return public_server(doc)


@router.patch("/v1/servers/{server_id}")
async def patch_server(server_id: str, request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
    changes = parse_server_patch(await json_object(request))
    store = get_store(request)
    await store.touch_account(caller.key)
    doc = await store.update_server(caller.key, server_id, changes)
    if doc is None:
        raise _not_found()
    return public_server(doc)


@router.delete("/v1/servers/{server_id}", status_code=204)
async def delete_server(server_id: str, request: Request, caller: Caller = Depends(current_account)) -> Response:
    store = get_store(request)
    await store.touch_account(caller.key)
    if not await store.delete_server(caller.key, server_id):
        raise _not_found()
    return Response(status_code=204)


@router.put("/v1/servers/{server_id}/agents")
async def put_agents(server_id: str, request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
    agents = parse_agents(await json_object(request), request.app.state.config.max_agents_per_server)
    store = get_store(request)
    await store.touch_account(caller.key)
    doc = await store.update_server(caller.key, server_id, {"agents": agents})
    if doc is None:
        raise _not_found()
    return {"agents": doc["agents"]}


# Per-server tokens: the Cloud is an OIDC issuer of its own (public_url), whose tokens are meant for one server.
@router.get("/.well-known/openid-configuration")
async def discovery(request: Request) -> dict[str, Any]:
    signing_key(request)
    public_url = request.app.state.config.public_url
    return {
        "issuer": public_url,
        "jwks_uri": f"{public_url}/v1/jwks",
        "id_token_signing_alg_values_supported": ["ES256"],
        "response_types_supported": [],
    }


@router.get("/v1/jwks")
async def jwks(request: Request) -> JSONResponse:
    key = signing_key(request)
    return JSONResponse({"keys": [key.jwk()]}, headers={"Cache-Control": "public, max-age=3600"})


@router.post("/v1/server-tokens")
async def issue_server_token(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    key = signing_key(request)
    caller = await current_account(request, authorization)
    config: CloudConfig = request.app.state.config
    audience = parse_audience(await json_object(request), config)
    # A token for a Cloud node would be a per-server credential where the node expects its own grants.
    if await NodeStore(get_cloud(request)).is_node_url(audience):
        raise ApiError("invalid", "audience must be a server, not a wristcall Cloud node", 422)
    claims = server_token_claims(
        issuer=config.public_url,
        account=caller.key,
        client_id=caller.client_id,
        audience=audience,
        now=time.time(),
        ttl_s=config.server_token_ttl_s,
    )
    token = key.sign(claims)
    # Neither the audience (it says where the user has a server) nor the token reach the log.
    log.info("server token issued (client %s)", caller.client_id)
    return {"token": token, "audience": audience, "expires_at": claims["exp"]}


@router.get("/v1/agents")
async def list_agents(request: Request, caller: Caller = Depends(current_account)) -> dict[str, Any]:
    return {
        "agents": [
            {**agent, "server_id": s["_id"], "server_name": s["name"], "server_url": s["url"]}
            for s in await get_store(request).list_servers(caller.key)
            for agent in s["agents"]
        ]
    }


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:  # the task died earlier; what is closed after it must close all the same
        log.warning("a purge task had stopped (%s)", type(e).__name__)


def create_app(
    config: CloudConfig,
    *,
    store: Store | None = None,
    verifier: Verifier | None = None,
    http: httpx.AsyncClient | None = None,
    signing_key: SigningKey | None = None,
    channels: dict[str, Channel] | None = None,
    users: Users | None = None,
) -> FastAPI:
    # httpx logs every request URL at INFO, and some URLs the Cloud calls are secrets; HTTP/2 (h2, hpack) logs
    # every header at DEBUG, the APNs request path (the device token) included; pymongo logs every command and
    # reply at DEBUG, documents included (push channels: APNs tokens, Web Push endpoints).
    for name in ("httpx", "httpcore", "h2", "hpack", "pymongo"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if signing_key is None and config.signing_key_pem is not None:
        signing_key = SigningKey(config.signing_key_pem)
    if signing_key is not None and not config.public_url:
        raise ConfigError("WRISTCALL_CLOUD_PUBLIC_URL and WRISTCALL_CLOUD_SIGNING_KEY go together")
    if config.push_fake:
        check_push_fake(config.public_url)
    if config.public_url:
        check_public_url(config.public_url, config.issuer)
    vapid = vapid_identity(config.vapid_private_pem, config.vapid_subject)
    provider_token = apns_provider_token(
        config.apns_key_pem, config.apns_key_id, config.apns_team_id, config.apns_topics
    )
    webpush_http: httpx.AsyncClient | None = None
    apns_http: httpx.AsyncClient | None = None
    if channels is None:
        channels = {}
        if config.push_fake:
            fake = FakeChannel()
            channels = {"apns": fake, "webpush": fake}
        else:
            # Each channel has a client of its own: never follows redirects, and nothing else shares its connections.
            if vapid is not None:
                webpush_http = httpx.AsyncClient(follow_redirects=False)
                channels["webpush"] = WebPushChannel(vapid, webpush_http, config.webpush_hosts)
            if provider_token is not None:
                apns_http = httpx.AsyncClient(http2=True, follow_redirects=False)
                channels["apns"] = ApnsChannel(provider_token, apns_http, hosts=config.apns_hosts)
    owned_http: httpx.AsyncClient | None = None
    if verifier is None:
        if not config.clients:
            raise ConfigError("at least one client id is required")
        if http is None:
            http = owned_http = httpx.AsyncClient()
        client_ids = list(config.clients.values())
        # Zitadel names the project in `aud` (not the client) when the app asks for the project audience scope, as
        # /v1/config tells it to; the token must still be issued to one of the app clients.
        audiences = client_ids + ([config.project_id] if config.project_id else [])
        verifier = OidcVerifier(config.issuer, audiences, http, clients=client_ids)
    if users is None and config.zitadel_client_id and config.zitadel_org_id and config.zitadel_client_secret:
        if http is None:
            http = owned_http = httpx.AsyncClient()
        users = ZitadelUsers(
            config.issuer, config.zitadel_org_id, config.zitadel_client_id, config.zitadel_client_secret, http
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # On the way out, what was pushed is stopped or closed in the reverse order, each one even when one before
        # it failed (the failures are raised after all of them ran).
        async with AsyncExitStack() as stack:
            for client in (owned_http, webpush_http, apns_http):
                if client is not None:
                    stack.push_async_callback(client.aclose)
            current = store
            if current is None:
                mongo, current = open_store(config)
                stack.push_async_callback(mongo.close)
            app.state.store = current
            await current.ensure_indexes()
            accounts = AccountStore(current.db)
            await accounts.ensure_indexes()
            created = await accounts.migrate()
            log.info("migration: %d identities created", created)
            app.state.accounts = accounts
            cloud = CloudModeStore(current.db)
            await cloud.ensure_indexes()
            app.state.cloud = cloud
            if config.legacy_zitadel_tokens:
                log.warning("legacy Zitadel tokens accepted on account routes (WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS)")
            purger = asyncio.create_task(
                purge_idle_loop(current, every_s=config.push_cleanup_every_s, idle_s=config.push_idle_days * 86400)
            )
            stack.push_async_callback(_stop, purger)
            account_purger = asyncio.create_task(
                purge_idle_accounts_loop(
                    accounts, every_s=config.account_cleanup_every_s, idle_s=config.account_idle_days * 86400
                )
            )
            stack.push_async_callback(_stop, account_purger)
            yield

    app = FastAPI(title="wristcall-cloud", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.verifier = verifier
    app.state.users = users
    app.state.signing_key = signing_key
    # Set again by the lifespan (on the Store it opens); set here too for an app driven without its lifespan.
    app.state.accounts = AccountStore(store.db) if store is not None else None
    app.state.cloud = CloudModeStore(store.db) if store is not None else None
    app.state.account_tokens = (
        AccountTokens(signing_key, config.public_url, ttl_s=config.access_token_ttl_s)
        if signing_key is not None
        else None
    )
    app.state.channels = channels
    app.state.send_limiter = SendLimiter(config.push_per_minute, config.push_per_day)
    app.state.registration_limiter = RegistrationLimiter(config.registrations_per_minute_per_ip)
    app.state.account_limiter = AccountCreationLimiter(config.accounts_per_ip_hour, config.accounts_per_ip_day)
    app.state.attempt_limiter = AttemptLimiter(config.session_attempts_per_minute)
    app.state.poll_limiter = PollLimiter()
    app.state.node_limiter = NodeLimiter(config.node_requests_per_minute)
    if config.push_fake:
        log.warning("push: fake channel, nothing is delivered (WRISTCALL_CLOUD_PUSH_FAKE)")
    app.add_middleware(BodyLimit, limit=MAX_BODY)
    if config.cors_origins:
        # Added last, so the outermost layer: errors raised by the layers inside (401, 413, 422) carry the headers
        # too. Bearer tokens in a header, no cookies: no credentials. A 500 is answered outside every middleware
        # and reaches the browser without them.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
            expose_headers=["Retry-After"],
            max_age=600,
        )

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, e: ApiError) -> JSONResponse:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after is not None else None
        return _error(e.code, e.message, e.status, headers)

    @app.exception_handler(AgendaError)
    async def agenda_error(_request: Request, e: AgendaError) -> JSONResponse:
        return _error(e.code, e.message, _STATUS[e.code])

    @app.exception_handler(PushError)
    async def push_error(_request: Request, e: PushError) -> JSONResponse:
        return push_error_response(e)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, e: RequestValidationError) -> JSONResponse:
        return _error("invalid", "the request is not valid", 422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, e: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}
        message = e.detail if isinstance(e.detail, str) else "request failed"
        return JSONResponse(
            {"error": codes.get(e.status_code, "http_error"), "message": message},
            status_code=e.status_code,
            headers=getattr(e, "headers", None),
        )

    app.include_router(router)
    app.include_router(sessions_router)
    app.include_router(push_router)
    app.include_router(node_me_router)
    app.include_router(node_agents_router)
    app.include_router(node_calls_router)

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/config")
    async def public() -> dict[str, Any]:
        return public_config(
            config,
            server_tokens=signing_key is not None,
            channels=app.state.channels,
            vapid_public_key=vapid.public_key if vapid is not None else None,
        )

    return app
