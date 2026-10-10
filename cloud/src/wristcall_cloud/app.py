"""HTTP API of the cloud. Callers authenticate with a central account access token (`Authorization: Bearer`), except
on the push relay (push/api.py), whose keys are their own credentials.

Every error is `{"error": code, "message": ...}`. Request bodies are capped at MAX_BODY bytes. Rate limiting is
left to the reverse proxy of the deployment (see README), except for the push relay's own limits (push/limits.py).
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pymongo.errors import DuplicateKeyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .agenda import AgendaError, parse_agents, parse_new_server, parse_server_patch
from .audience import AudienceError, is_loopback, normalize_audience
from .config import CloudConfig, ConfigError, check_public_url, check_push_fake, same_url
from .oidc import Identity, OidcError, OidcUnavailable, OidcVerifier
from .push.api import push_error_response
from .push.api import router as push_router
from .push.channels import Channel, FakeChannel
from .push.limits import RegistrationLimiter, SendLimiter
from .push.registry import PushError, purge_idle_loop
from .signing import SigningKey, server_token_claims
from .store import Store, open_store, public_server

log = logging.getLogger("wristcall_cloud.app")

MAX_BODY = 64 * 1024
BASE_SCOPES = ["openid", "profile", "offline_access"]


class Verifier(Protocol):
    async def verify(self, token: str) -> Identity: ...


@dataclass(frozen=True)
class Caller:
    key: str  # "<issuer>#<subject>": the account's id in this service
    client_id: str | None


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


class BodyTooLarge(HTTPException):
    """An HTTPException on purpose: FastAPI re-raises those while reading a model body, but turns anything else
    into 400."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=f"request body is limited to {MAX_BODY} bytes")


class BodyLimit:
    """Rejects bodies over `limit` bytes: up front by Content-Length, otherwise while the body is read."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length" and (not value.isdigit() or int(value) > self.limit):
                await self._reject(scope, receive, send)
                return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise BodyTooLarge()
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
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        await _error("too_large", BodyTooLarge().detail, 413)(scope, receive, send)


async def current_account(request: Request, authorization: str | None = Header(default=None)) -> Caller:
    """Dependency: the central account behind the bearer token. The token never reaches a log or a response."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ApiError("unauthorized", "missing or invalid token", 401)
    verifier: Verifier = request.app.state.verifier
    try:
        identity = await verifier.verify(token)
    except OidcUnavailable as e:
        log.warning("central account unavailable: %s", e)
        raise ApiError("account_unavailable", "the central account is unavailable; try again later", 503) from None
    except OidcError as e:
        log.info("token rejected: %s", e)
        raise ApiError("unauthorized", "missing or invalid token", 401) from None
    return Caller(key=f"{identity.issuer}#{identity.subject}", client_id=identity.client_id)


async def json_object(request: Request) -> dict[str, Any]:
    """The request body as a JSON object (422 `invalid` otherwise); size already capped by BodyLimit."""
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        raise ApiError("invalid", "body must be valid JSON", 422) from None
    if not isinstance(body, dict):
        raise ApiError("invalid", "body must be a JSON object", 422)
    return body


def get_store(request: Request) -> Store:
    return request.app.state.store


def public_config(
    config: CloudConfig, *, server_tokens: bool = False, channels: dict[str, Channel] | None = None
) -> dict[str, Any]:
    scopes = list(BASE_SCOPES)
    if config.project_id:
        scopes.append(f"urn:zitadel:iam:org:project:id:{config.project_id}:aud")
    return {
        "issuer": config.issuer,
        "project_id": config.project_id,
        "clients": dict(config.clients),
        "scopes": scopes,
        "server_tokens": server_tokens,
        "push": {
            "apns": "apns" in (channels or {}),
            "webpush": "webpush" in (channels or {}),
            "vapid_public_key": None,
            "apns_topics": list(config.apns_topics),
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
    return {
        "account": caller.key,
        "created_at": account["created_at"],
        "servers": await store.count_servers(caller.key),
    }


@router.delete("/v1/account", status_code=204)
async def delete_account(request: Request, caller: Caller = Depends(current_account)) -> Response:
    clients = request.app.state.config.clients
    # Only the apps may delete the account; a token issued to the watch (or anyone else) may not.
    if caller.client_id is None or caller.client_id not in (clients.get("ios"), clients.get("pwa")):
        raise ApiError("forbidden", "this client cannot delete the account", 403)
    await get_store(request).delete_account(caller.key)
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


def create_app(
    config: CloudConfig,
    *,
    store: Store | None = None,
    verifier: Verifier | None = None,
    http: httpx.AsyncClient | None = None,
    signing_key: SigningKey | None = None,
    channels: dict[str, Channel] | None = None,
) -> FastAPI:
    # httpx logs every request URL at INFO, and some URLs the Cloud calls are secrets; pymongo logs every command
    # and reply at DEBUG, documents included (push channels: APNs tokens, Web Push endpoints).
    for name in ("httpx", "httpcore", "pymongo"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if signing_key is None and config.signing_key_pem is not None:
        signing_key = SigningKey(config.signing_key_pem)
    if signing_key is not None and not config.public_url:
        raise ConfigError("WRISTCALL_CLOUD_PUBLIC_URL and WRISTCALL_CLOUD_SIGNING_KEY go together")
    if config.push_fake:
        check_push_fake(config.public_url)
    if config.public_url:
        check_public_url(config.public_url, config.issuer)
    if channels is None:
        channels = {}
        if config.push_fake:
            fake = FakeChannel()
            channels = {"apns": fake, "webpush": fake}
    owned_http: httpx.AsyncClient | None = None
    if verifier is None:
        if not config.clients:
            raise ConfigError("at least one client id is required")
        if http is None:
            http = owned_http = httpx.AsyncClient()
        client_ids = list(config.clients.values())
        verifier = OidcVerifier(config.issuer, client_ids, http, clients=client_ids)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongo = None
        current = store
        if current is None:
            mongo, current = open_store(config)
        app.state.store = current
        purger: asyncio.Task[None] | None = None
        try:
            await current.ensure_indexes()
            purger = asyncio.create_task(
                purge_idle_loop(current, every_s=config.push_cleanup_every_s, idle_s=config.push_idle_days * 86400)
            )
            yield
        finally:
            if purger is not None:
                purger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await purger
            if mongo is not None:
                await mongo.close()
            if owned_http is not None:
                await owned_http.aclose()

    app = FastAPI(title="wristcall-cloud", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.verifier = verifier
    app.state.signing_key = signing_key
    app.state.channels = channels
    app.state.send_limiter = SendLimiter(config.push_per_minute, config.push_per_day)
    app.state.registration_limiter = RegistrationLimiter(config.registrations_per_minute_per_ip)
    if config.push_fake:
        log.warning("push: fake channel, nothing is delivered (WRISTCALL_CLOUD_PUSH_FAKE)")
    app.add_middleware(BodyLimit, limit=MAX_BODY)

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, e: ApiError) -> JSONResponse:
        return _error(e.code, e.message, e.status)

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
    app.include_router(push_router)

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/config")
    async def public() -> dict[str, Any]:
        return public_config(config, server_tokens=signing_key is not None, channels=app.state.channels)

    return app
