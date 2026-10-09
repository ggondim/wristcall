"""HTTP app: pairing (REST) and calls (WebSocket /v1/call). Protocol in docs/protocol.md."""

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, protocol
from .agents import AgentError, AgentService, agent_summary, build_agent_providers
from .api import management_router
from .auth import Authenticator, Principal
from .bootstrap import bootstrap, log_report
from .config import AppConfig
from .pairing import Paired, PairingDenied, PairingGone, PairingService
from .providers import ProviderError, check_providers
from .ratelimit import RateLimiter
from .session import CallSession
from .storage import Storage, open_sqlite_storage
from .vad import build_vad
from .warmup import run_background, warm_all, warmup_targets

log = logging.getLogger("wristcall.app")


class PairBody(BaseModel):
    code: str | None = None
    device_name: str = Field(default="watch", max_length=64)


class PollBody(BaseModel):
    poll_token: str = Field(min_length=1, max_length=128)


class _WsTransport:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._lock = asyncio.Lock()

    async def send_json(self, msg: dict[str, Any]) -> None:
        async with self._lock:
            await self._ws.send_text(json.dumps(msg, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        async with self._lock:
            await self._ws.send_bytes(data)


def create_app(
    config: AppConfig,
    *,
    storage: Storage | None = None,
    http: httpx.AsyncClient | None = None,
    start_timeout_s: float = 10.0,
) -> FastAPI:
    own_http = http is None
    http_client = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    check_providers(config, http_client)
    store = storage or open_sqlite_storage(config.server.data_dir)
    pairing_svc = PairingService(
        store, config.server.pairing_approval, max_devices_per_user=config.limits.max_devices_per_user
    )
    agents = AgentService(store, config, http_client)
    auth = Authenticator(store)
    limiter = RateLimiter(limit=10, window_s=60)
    targets = warmup_targets(config, http_client)
    call_targets = [t for t in targets if t.config.on_call]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log_report(await bootstrap(store, config))
        background = asyncio.create_task(run_background(targets)) if targets else None
        yield
        if background is not None:
            background.cancel()
            with suppress(asyncio.CancelledError):
                await background
        if own_http:
            await http_client.aclose()

    app = FastAPI(title="wristcall", version=__version__, lifespan=lifespan)
    app.state.storage = store
    app.state.pairing = pairing_svc
    app.state.agents = agents
    app.state.auth = auth
    app.include_router(management_router(config, auth, agents, pairing_svc))

    def client_ip(request: Request) -> str:
        header = config.server.client_ip_header
        if header and (value := request.headers.get(header)):
            return value.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def device_from(authorization: str | None) -> Principal | None:
        principal = await auth.authenticate(authorization)
        return principal if principal is not None and principal.kind == "device" else None

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "protocol": protocol.PROTOCOL_VERSION}

    @app.post("/v1/pair")
    async def pair(body: PairBody, request: Request) -> Any:
        if not limiter.allow(client_ip(request)):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        try:
            result = await pairing_svc.pair(body.code, body.device_name)
        except PairingDenied as e:
            return JSONResponse({"error": "invalid_code", "message": str(e)}, status_code=401)
        if isinstance(result, Paired):
            log.info("device paired: %s", result.device_id)
            return {"device_id": result.device_id, "token": result.token}
        return JSONResponse(
            {"request_id": result.request_id, "poll_token": result.poll_token, "expires_at": result.expires_at},
            status_code=202,
        )

    @app.post("/v1/pair/poll")
    async def poll(body: PollBody) -> Any:
        # The poll_token is a secret: it goes in the body, not in the path, so it does not show up in the access log.
        try:
            result = await pairing_svc.poll(body.poll_token)
        except PairingGone as e:
            return JSONResponse({"error": "gone", "message": str(e)}, status_code=410)
        if isinstance(result, Paired):
            log.info("device paired by approval: %s", result.device_id)
            return {"device_id": result.device_id, "token": result.token}
        return JSONResponse({"request_id": result.request_id, "expires_at": result.expires_at}, status_code=202)

    @app.get("/v1/me")
    async def me(authorization: str | None = Header(default=None)) -> Any:
        principal = await device_from(authorization)
        if principal is None or principal.device is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        user = await store.users.get(principal.user_id)
        listed = await agents.list(principal.user_id)
        return {
            "device_id": principal.device.id,
            "device_name": principal.device.name,
            "user": {"id": user.id, "handle": user.handle, "display_name": user.display_name} if user else None,
            "agents": [agent_summary(a) for a in listed],
            # 0.2.0 shape, read by watch 0.1.0 (it calls the first one).
            "profiles": [{"name": a.slug, "display_name": a.display_name} for a in listed],
        }

    @app.delete("/v1/me")
    async def unpair(authorization: str | None = Header(default=None)) -> Any:
        principal = await device_from(authorization)
        if principal is None or principal.device is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        await pairing_svc.revoke(principal.device.id)
        log.info("device unpaired: %s", principal.device.id)
        return Response(status_code=204)

    async def fatal(ws: WebSocket, err: protocol.ProtocolError) -> None:
        with suppress(Exception):
            await ws.send_text(json.dumps(protocol.error(err.code, err.message, True), ensure_ascii=False))
            await ws.close(code=protocol.CLOSE_PROTOCOL_ERROR)

    @app.websocket("/v1/call")
    async def call(ws: WebSocket) -> None:
        await ws.accept()
        principal = await device_from(ws.headers.get("authorization"))
        if principal is None or principal.device is None:
            await ws.close(code=protocol.CLOSE_UNAUTHORIZED)
            return
        try:
            first = await asyncio.wait_for(ws.receive(), start_timeout_s)
        except TimeoutError:
            await fatal(ws, protocol.ProtocolError(protocol.ErrorCode.NOT_STARTED, "session.start did not arrive in time"))
            return
        if first["type"] == "websocket.disconnect":
            return
        try:
            text = first.get("text")
            if text is None:
                raise protocol.ProtocolError(protocol.ErrorCode.NOT_STARTED, "the first message must be session.start")
            start = protocol.parse_client_message(text)
            if not isinstance(start, protocol.SessionStart):
                raise protocol.ProtocolError(protocol.ErrorCode.NOT_STARTED, "the first message must be session.start")
            protocol.check_session_start(start)
            ref = start.agent or start.profile
            if ref is None:
                agent = await agents.default(principal.user_id)
                if agent is None:
                    raise protocol.ProtocolError(protocol.ErrorCode.UNKNOWN_PROFILE, "this account has no agents yet")
            else:
                try:
                    agent = await agents.get(principal.user_id, ref)
                except AgentError:
                    raise protocol.ProtocolError(protocol.ErrorCode.UNKNOWN_PROFILE, f"unknown agent: {ref}") from None
            try:
                provider_set = build_agent_providers(config, agent.spec, http_client)
            except ProviderError as e:
                log.warning("agent %s unavailable: %s", agent.id, e)
                raise protocol.ProtocolError(
                    protocol.ErrorCode.AGENT_UNAVAILABLE, "this agent is not available; check its providers"
                ) from None
        except protocol.ProtocolError as e:
            await fatal(ws, e)
            return

        # Clients that name the agent get its mode; 0.2.x clients (profile or nothing) keep 0.2.0's default,
        # because watch 0.1.0 omits turn_end when the user picks auto.
        turn_end = start.turn_end or (agent.spec.turn_end if start.agent else "auto")
        transport = _WsTransport(ws)
        session = CallSession(agent.spec, provider_set, build_vad(agent.spec.vad), transport, turn_end=turn_end)
        session_id = secrets.token_hex(8)
        await transport.send_json(
            protocol.session_ready(
                session_id, agent_summary(agent), turn_end, protocol.AudioFormat(sample_rate=provider_set.tts.sample_rate)
            )
        )
        log.info(
            "call %s started: device=%s agent=%s (%s) turn_end=%s",
            session_id, principal.device.id, agent.id, agent.slug, turn_end,
        )
        warming = asyncio.create_task(warm_all(call_targets)) if call_targets else None
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    await session.on_audio(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    msg = protocol.parse_client_message(text)
                except protocol.ProtocolError as e:
                    await transport.send_json(protocol.error(e.code, e.message, False))
                    continue
                if isinstance(msg, protocol.Mute):
                    await session.on_mute(msg.muted)
                elif isinstance(msg, protocol.SessionEnd):
                    break
                else:
                    await transport.send_json(protocol.error(protocol.ErrorCode.BAD_MESSAGE, "session already started", False))
        finally:
            if warming is not None and not warming.done():
                warming.cancel()
            await session.close()
            with suppress(Exception):
                await ws.close(code=protocol.CLOSE_NORMAL)
            log.info("call %s ended", session_id)

    return app
