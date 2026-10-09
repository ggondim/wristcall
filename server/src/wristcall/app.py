"""HTTP app: pairing (REST), calls (WebSocket /v1/call) and their status. Protocol in docs/protocol.md."""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, protocol
from .account import AccountService
from .agents import (
    ONE_WAY, Agent, AgentError, AgentService, OneWayProviders, agent_summary, build_agent_providers,
    build_one_way_providers,
)
from .api import management_router
from .auth import Authenticator, Principal
from .bootstrap import bootstrap, log_report
from .config import AppConfig
from .delivery import DeliveryPolicy
from .oneway import Background, OneWayCall, call_view, new_call_id
from .pairing import Paired, PairingDenied, PairingGone, PairingService
from .providers import ProviderError, check_providers
from .ratelimit import RateLimiter
from .session import CallSession
from .storage import CallRecord, Storage, open_sqlite_storage
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
    delivery_policy: DeliveryPolicy = DeliveryPolicy(),
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
    account = AccountService(store, config.central_account, http_client) if config.central_account else None
    targets = warmup_targets(config, http_client)
    call_targets = [t for t in targets if t.config.on_call]
    after_calls = Background()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log_report(await bootstrap(store, config))
        # Only the server does this (the CLI may run next to a live server): its own calls died with it.
        if interrupted := await store.calls.interrupt_unfinished(time.time()):
            log.warning("%d one-way call(s) left unfinished by the last run marked as interrupted", interrupted)
        background = asyncio.create_task(run_background(targets)) if targets else None
        yield
        await after_calls.close()
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
    app.state.account = account

    def client_ip(request: Request) -> str:
        header = config.server.client_ip_header
        if header and (value := request.headers.get(header)):
            return value.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    app.include_router(
        management_router(config, auth, agents, pairing_svc, account, limiter=limiter, client_ip=client_ip)
    )

    async def device_from(authorization: str | None) -> Principal | None:
        principal = await auth.authenticate(authorization)
        return principal if principal is not None and principal.kind == "device" else None

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        central = config.central_account
        return {
            "status": "ok",
            "version": __version__,
            "protocol": protocol.PROTOCOL_VERSION,
            "account": {"issuer": central.issuer, "device_credential": central.device_credential} if central else None,
        }

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

    @app.get("/v1/calls/{call_id}")
    async def call_status(call_id: str, authorization: str | None = Header(default=None)) -> Any:
        # Device or API token: a user sees only their own calls.
        principal = await auth.authenticate(authorization)
        if principal is None:
            return JSONResponse({"error": "unauthorized", "message": "missing or invalid token"}, status_code=401)
        record = await store.calls.get(principal.user_id, call_id)
        if record is None:
            return JSONResponse({"error": "not_found", "message": "call not found"}, status_code=404)
        return call_view(record)

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

    async def pump(
        ws: WebSocket,
        transport: "_WsTransport",
        on_audio: Callable[[bytes], Awaitable[bool]],
        on_mute: Callable[[bool], Awaitable[None]],
    ) -> None:
        """Feeds the client's messages to the call until hang-up, disconnect, or on_audio returns True."""
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                if await on_audio(data):
                    return
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
                await on_mute(msg.muted)
            elif isinstance(msg, protocol.SessionEnd):
                return
            else:
                await transport.send_json(protocol.error(protocol.ErrorCode.BAD_MESSAGE, "session already started", False))

    async def one_way_call(ws: WebSocket, device_id: str, agent: Agent, providers: OneWayProviders) -> None:
        """Records until hang-up (or the limit); transcription and delivery go on after the WebSocket closes."""
        # Before the record exists: a VAD that fails to load must not leave a call stuck in "recording".
        vad = build_vad(agent.spec.vad)
        now = time.time()
        record = await store.calls.create(CallRecord(
            id=new_call_id(), user_id=agent.user_id, agent_id=agent.id, device_id=device_id,
            call_type=agent.call_type, status="recording", created_at=now, updated_at=now,
        ))
        call = OneWayCall(
            agent, providers, vad, record, store,
            max_call_ms=config.limits.max_one_way_call_s * 1000, policy=delivery_policy,
        )
        transport = _WsTransport(ws)
        warming: asyncio.Task[None] | None = None

        async def on_audio(data: bytes) -> bool:
            call.on_audio(data)
            if call.captured:
                await transport.send_json(protocol.call_captured(record.id, "limit"))
            return call.captured

        async def on_mute(muted: bool) -> None:
            call.on_mute(muted)

        # From here on the record exists: whatever happens (even the client gone before session.ready), it gets finished.
        try:
            # No voice comes back: audio_out is nominal (watch 0.1.0 requires it). Silence never ends the call.
            await transport.send_json(protocol.session_ready(
                secrets.token_hex(8), agent_summary(agent), "manual",
                protocol.AudioFormat(sample_rate=protocol.INPUT_SAMPLE_RATE), call_id=record.id,
            ))
            log.info("call %s started: device=%s agent=%s (%s) type=%s", record.id, device_id, agent.id, agent.slug, agent.call_type)
            warming = asyncio.create_task(warm_all(call_targets)) if call_targets else None
            await pump(ws, transport, on_audio, on_mute)
        finally:
            if warming is not None and not warming.done():
                warming.cancel()
            after_calls.spawn(call.finish())
            with suppress(Exception):
                await ws.close(code=protocol.CLOSE_NORMAL)
            log.info("call %s recorded", record.id)

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
                if agent.call_type in ONE_WAY:
                    one_way = build_one_way_providers(config, agent.spec, http_client)
                else:
                    provider_set = build_agent_providers(config, agent.spec, http_client)
            except ProviderError as e:
                log.warning("agent %s unavailable: %s", agent.id, e)
                raise protocol.ProtocolError(
                    protocol.ErrorCode.AGENT_UNAVAILABLE, "this agent is not available; check its providers"
                ) from None
        except protocol.ProtocolError as e:
            await fatal(ws, e)
            return

        if agent.call_type in ONE_WAY:
            await one_way_call(ws, principal.device.id, agent, one_way)
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

        async def on_audio(data: bytes) -> bool:
            await session.on_audio(data)
            return False

        try:
            await pump(ws, transport, on_audio, session.on_mute)
        finally:
            if warming is not None and not warming.done():
                warming.cancel()
            await session.close()
            with suppress(Exception):
                await ws.close(code=protocol.CLOSE_NORMAL)
            log.info("call %s ended", session_id)

    return app
