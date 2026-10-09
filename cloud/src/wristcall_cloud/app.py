"""HTTP API of the cloud. Callers authenticate with a central account access token (`Authorization: Bearer`).

Every error is `{"error": code, "message": ...}`. Request bodies are capped at MAX_BODY bytes. Rate limiting is
left to the reverse proxy of the deployment (see README).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import CloudConfig, ConfigError
from .oidc import Identity, OidcError, OidcUnavailable, OidcVerifier
from .store import Store, open_store

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


def public_config(config: CloudConfig) -> dict[str, Any]:
    scopes = list(BASE_SCOPES)
    if config.project_id:
        scopes.append(f"urn:zitadel:iam:org:project:id:{config.project_id}:aud")
    return {"issuer": config.issuer, "project_id": config.project_id, "clients": dict(config.clients), "scopes": scopes}


def create_app(
    config: CloudConfig,
    *,
    store: Store | None = None,
    verifier: Verifier | None = None,
    http: httpx.AsyncClient | None = None,
) -> FastAPI:
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
        try:
            await current.ensure_indexes()
            yield
        finally:
            if mongo is not None:
                await mongo.close()
            if owned_http is not None:
                await owned_http.aclose()

    app = FastAPI(title="wristcall-cloud", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.verifier = verifier
    app.add_middleware(BodyLimit, limit=MAX_BODY)

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, e: ApiError) -> JSONResponse:
        return _error(e.code, e.message, e.status)

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

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/config")
    async def public() -> dict[str, Any]:
        return public_config(config)

    return app
