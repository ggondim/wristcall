"""Management API (REST, JSON): agents, devices, pairing codes and the providers on offer.

Authentication: a user's API token (`wc_pat_...`, from `wristcall users tokens add`). A paired device may only
list its user's agents (GET /v1/agents and GET /v1/agents/{ref}, summary view). The central account's token is
never a credential here: /v1/account/link takes it in the body, next to a local proof.
"""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from .account import AccountError, AccountService
from .agents import AgentError, AgentService, agent_detail, agent_summary
from .auth import Authenticator, Principal
from .config import AppConfig
from .directory_client import DirectoryClient, DirectoryError
from .pairing import DeviceLimit, PairingService, issue_code
from .providers import provider_kind
from .ratelimit import RateLimiter

log = logging.getLogger("wristcall.api")

_STATUS = {"invalid": 422, "unsupported": 422, "not_found": 404, "conflict": 409, "limit": 403}
_ACCOUNT_STATUS = {
    "invalid_account_token": 401, "account_unavailable": 503, "conflict": 409, "not_found": 404, "invalid_code": 401,
}


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _unauthorized() -> JSONResponse:
    return _error("unauthorized", "missing or invalid token", 401)


def _forbidden() -> JSONResponse:
    return _error("forbidden", "this needs a user API token, not a device token", 403)


async def _json_object(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        return _error("invalid", "body must be valid JSON", 422)
    if not isinstance(body, dict):
        return _error("invalid", "body must be a JSON object", 422)
    return body


def _agent_error(e: AgentError) -> JSONResponse:
    return _error(e.code, e.message, _STATUS.get(e.code, 422))


def _not_configured() -> JSONResponse:
    return _error("not_configured", "this server is not linked to a central account", 404)


def _account_error(e: AccountError) -> JSONResponse:
    return _error(e.code, e.message, _ACCOUNT_STATUS.get(e.code, 401))


def management_router(
    config: AppConfig,
    auth: Authenticator,
    agents: AgentService,
    pairing: PairingService,
    account: AccountService | None = None,
    *,
    limiter: RateLimiter | None = None,
    client_ip: Callable[[Request], str] | None = None,
) -> APIRouter:
    """`limiter` and `client_ip` are /v1/pair's: linking with a code spends the same per-IP budget."""
    router = APIRouter(prefix="/v1")
    directory = DirectoryClient(config.server.directory_url) if config.server.directory_url else None

    async def principal(authorization: str | None) -> Principal | JSONResponse:
        found = await auth.authenticate(authorization)
        return found if found is not None else _unauthorized()

    async def owner(authorization: str | None) -> Principal | JSONResponse:
        found = await principal(authorization)
        if isinstance(found, Principal) and found.kind != "api":
            return _forbidden()
        return found

    @router.get("/agents")
    async def list_agents(authorization: str | None = Header(default=None)) -> Any:
        who = await principal(authorization)
        if isinstance(who, JSONResponse):
            return who
        view = agent_detail if who.kind == "api" else agent_summary
        return {"agents": [view(a) for a in await agents.list(who.user_id)]}

    @router.post("/agents")
    async def create_agent(request: Request, authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        body = await _json_object(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            agent = await agents.create(who.user_id, body)
        except AgentError as e:
            return _agent_error(e)
        return JSONResponse(agent_detail(agent), status_code=201)

    @router.get("/agents/{ref}")
    async def get_agent(ref: str, authorization: str | None = Header(default=None)) -> Any:
        who = await principal(authorization)
        if isinstance(who, JSONResponse):
            return who
        try:
            agent = await agents.get(who.user_id, ref)
        except AgentError as e:
            return _agent_error(e)
        return agent_detail(agent) if who.kind == "api" else agent_summary(agent)

    @router.patch("/agents/{ref}")
    async def update_agent(ref: str, request: Request, authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        body = await _json_object(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            return agent_detail(await agents.update(who.user_id, ref, body))
        except AgentError as e:
            return _agent_error(e)

    @router.delete("/agents/{ref}")
    async def delete_agent(ref: str, authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        try:
            await agents.delete(who.user_id, ref)
        except AgentError as e:
            return _agent_error(e)
        return Response(status_code=204)

    @router.get("/providers")
    async def list_providers(authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        # "webhook" is the action of one-shot and monologue agents.
        kinds = {"stt": "stt", "responder": "action", "tts": "tts", "webhook": "webhook"}
        return {
            "providers": [
                {"name": name, "kind": kinds[kind]}
                for name, p in config.providers.items()
                if (kind := provider_kind(p.type)) is not None
            ],
            "custom_endpoints": config.limits.custom_endpoints,
        }

    @router.get("/devices")
    async def list_devices(authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        return {
            "devices": [{"id": d.id, "name": d.name, "created_at": d.created_at} for d in await pairing.list_devices(who.user_id)]
        }

    @router.delete("/devices/{device_id}")
    async def revoke_device(device_id: str, authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        if not await pairing.revoke(device_id, user_id=who.user_id):
            return _error("not_found", f"device not found: {device_id}", 404)
        return Response(status_code=204)

    @router.post("/pairing-codes")
    async def create_pairing_code(authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        try:
            issued = await issue_code(pairing, who.user_id, config.server.public_url, directory)
        except DeviceLimit as e:
            return _error("limit", str(e), 403)
        except DirectoryError as e:
            return _error("directory", str(e), 502)
        body = {
            "code": issued.code.code,
            "expires_at": issued.code.expires_at,
            "server_url": config.server.public_url,
            "via_directory": issued.via_directory,
        }
        if issued.warning:
            body["warning"] = issued.warning
        return JSONResponse(body, status_code=201)

    @router.post("/account/link")
    async def link_account(request: Request, authorization: str | None = Header(default=None)) -> Any:
        # Proof of the central account in the body (`token`), proof of the local user in the Authorization
        # header (API token) or in the body (`code`). The body is never echoed: it holds both secrets.
        if account is None:
            return _not_configured()
        if limiter is not None and not limiter.allow(client_ip(request) if client_ip else "unknown"):
            return _error("rate_limited", "too many attempts; try again in a minute", 429)
        who = await owner(authorization) if authorization is not None else None
        if isinstance(who, JSONResponse):
            return who
        body = await _json_object(request)
        if isinstance(body, JSONResponse):
            return body
        token, code = body.get("token"), body.get("code")
        if not isinstance(token, str) or not token:
            return _error("invalid", "token: the central account access token is required", 422)
        if code is not None and not isinstance(code, str):
            return _error("invalid", "code: must be a string", 422)
        try:
            if who is not None:
                # The API token already proves the user; a code sent along is left unspent.
                await account.link(who.user_id, token)
                log.info("user %s linked to the central account (API token)", who.user_id)
                return {"linked": True, "issuer": account.config.issuer}
            if code is None:
                return _error("unauthorized", "send a user API token or a pairing code of this server", 401)
            user_id, _key = await account.link_with_code(code, token)
            user, api_token = await account.issue_app_token(user_id)
        except AccountError as e:
            return _account_error(e)
        log.info("user %s linked to the central account (pairing code)", user_id)
        return {
            "linked": True,
            "issuer": account.config.issuer,
            "user": {"id": user.id, "handle": user.handle},
            "api_token": api_token,
        }

    @router.delete("/account/link")
    async def unlink_account(authorization: str | None = Header(default=None)) -> Any:
        if account is None:
            return _not_configured()
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        if not await account.unlink(who.user_id):
            return _error("not_found", "this user is not linked to a central account", 404)
        log.info("user %s unlinked from the central account", who.user_id)
        return Response(status_code=204)

    return router
