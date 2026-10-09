"""Management API (REST, JSON): agents, devices, pairing codes and the providers on offer.

Authentication: a user's API token (`wc_pat_...`, from `wristcall users tokens add`). A paired device may only
list its user's agents (GET /v1/agents and GET /v1/agents/{ref}, summary view).
"""

from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from .agents import AgentError, AgentService, agent_detail, agent_summary
from .auth import Authenticator, Principal
from .config import AppConfig
from .directory_client import DirectoryClient, DirectoryError
from .pairing import DeviceLimit, PairingService, issue_code
from .providers import provider_kind

_STATUS = {"invalid": 422, "unsupported": 422, "not_found": 404, "conflict": 409, "limit": 403}


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


def management_router(
    config: AppConfig, auth: Authenticator, agents: AgentService, pairing: PairingService
) -> APIRouter:
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
        kinds = {"stt": "stt", "responder": "action", "tts": "tts"}
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

    return router
