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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, protocol
from .account import AccountError, AccountService
from .agents import (
    ONE_WAY, Agent, AgentError, AgentService, OneWayProviders, agent_summary, build_agent_providers,
    build_one_way_providers,
)
from .api import management_router
from .audience import AudienceError, normalize_audience
from .auth import Authenticator, Principal
from .bootstrap import bootstrap, log_report
from .config import AppConfig
from .delivery import DeliveryPolicy
from .history import History, check_key
from .history_api import calls_router
from .history_codec import HistoryCodec
from .oneway import Background, OneWayCall
from .pairing import DeviceLimit, Paired, PairingDenied, PairingGone, PairingService, Pending, clean_device_name
from .providers import ProviderError, check_providers
from .push import PushNotifier, push_router
from .ratelimit import RateLimiter
from .redelivery import Redelivery
from .session import CallSession
from .storage import Storage, open_sqlite_storage
from .vad import build_vad
from .warmup import run_background, warm_all, warmup_targets

log = logging.getLogger("wristcall.app")


class PairBody(BaseModel):
    code: str | None = None
    device_name: str = Field(default="watch", max_length=64)


class PairAccountBody(BaseModel):
    # No max_length on the token: a validation error would echo it back. The verifier caps its length.
    token: str
    device_name: str = Field(default="watch", max_length=64)


class PollBody(BaseModel):
    poll_token: str = Field(min_length=1, max_length=128)


async def purge_forever(history: History, every_s: float) -> None:
    """Deletes expired calls now and then every `every_s`. Only the server purges, never the CLI."""
    while True:
        try:
            if purged := await history.purge():
                log.info("history: %d expired call(s) deleted", purged)
        except Exception:
            log.exception("history: purge failed")
        await asyncio.sleep(every_s)


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


def _check_audience(config: AppConfig) -> None:
    """Not an error (the server may have another public name), but apps that reach it at `public_url` would
    ask the Cloud for tokens this server refuses."""
    try:
        public = normalize_audience(config.server.public_url)
    except AudienceError:
        public = None
    if public not in config.central_account.audience:
        log.warning(
            "server.public_url is not in central_account.audience: apps reaching the server there cannot pair "
            "with the central account"
        )


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
    if config.central_account:
        _check_audience(config)
    history = History(store, HistoryCodec(config.history.key()), config.history)
    targets = warmup_targets(config, http_client)
    call_targets = [t for t in targets if t.config.on_call]
    after_calls = Background()
    # Pushes run with the work after the calls: the shutdown gives them the same few seconds, then cancels them.
    notifier = PushNotifier(store, config.push, http_client) if config.push else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await check_key(store, history.codec)
        log_report(await bootstrap(store, config))
        await history.apply_retention()
        purging = asyncio.create_task(purge_forever(history, config.history.purge_every_s))
        # Only the server does this (the CLI may run next to a live server): its own calls died with it.
        if interrupted := await store.calls.interrupt_unfinished(time.time()):
            log.warning("%d call(s) left unfinished by the last run marked as interrupted", interrupted)
        background = asyncio.create_task(run_background(targets)) if targets else None
        yield
        purging.cancel()
        with suppress(asyncio.CancelledError):
            await purging
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
    app.state.history = history

    def client_ip(request: Request) -> str:
        header = config.server.client_ip_header
        if header and (value := request.headers.get(header)):
            return value.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    app.include_router(
        management_router(config, auth, agents, pairing_svc, account, limiter=limiter, client_ip=client_ip)
    )

    async def discard_at_relay(key: str) -> None:
        if notifier is not None:
            after_calls.spawn(notifier.discard(key))

    app.include_router(push_router(store, auth, config, on_discarded=discard_at_relay if notifier else None))

    redelivery = Redelivery(history, agents, config, http_client, policy=delivery_policy, background=after_calls)
    app.include_router(calls_router(auth, agents, history, redelivery.start))

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
            "push": {"relay": config.push.relay_url} if config.push else None,
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

    async def notify_approval(notifier: PushNotifier, user_id: str, pending: Pending, device_name: str) -> None:
        try:
            await notifier.approval_requested(user_id, pending.request_id, device_name, pending.expires_at)
        except Exception as e:  # noqa: BLE001 - the request is stored; a push failure only logs
            log.warning("pairing request %s: push failed: %s", pending.request_id, type(e).__name__)

    @app.post("/v1/pair/account")
    async def pair_account(body: PairAccountBody, request: Request) -> Any:
        # Public: the central account's access token in the body is the only proof (no Authorization header).
        # Neither the token nor the body is ever logged or echoed.
        if not limiter.allow(client_ip(request)):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        if account is None:
            return JSONResponse(
                {"error": "not_configured", "message": "this server is not linked to a central account"}, status_code=404
            )
        try:
            user = await account.user_for(body.token)
        except AccountError as e:
            status = 503 if e.code == "account_unavailable" else 401
            return JSONResponse({"error": e.code, "message": e.message}, status_code=status)
        if user is None:
            return JSONResponse(
                {"error": "not_linked", "message": "link this account to a user on the server first"}, status_code=403
            )
        approval = account.config.device_credential == "approval"
        try:
            result = await pairing_svc.pair_for_user(user.id, body.device_name, approval=approval)
        except DeviceLimit as e:  # a PairingDenied too: it must come first
            return JSONResponse({"error": "limit", "message": str(e)}, status_code=403)
        except PairingDenied as e:
            return JSONResponse({"error": "too_many_requests", "message": str(e)}, status_code=429)
        if isinstance(result, Paired):
            log.info("device paired via account: %s", result.device_id)
            return {"device_id": result.device_id, "token": result.token}
        log.info("pairing request for user %s", user.id)
        response = JSONResponse(
            {"request_id": result.request_id, "poll_token": result.poll_token, "expires_at": result.expires_at},
            status_code=202,
        )
        if notifier is not None:  # in the background: the 202 does not wait for the relay
            after_calls.spawn(notify_approval(notifier, user.id, result, clean_device_name(body.device_name)))
        return response

    @app.post("/v1/pair/poll")
    async def poll(body: PollBody) -> Any:
        # The poll_token is a secret: it goes in the body, not in the path, so it does not show up in the access log.
        try:
            result = await pairing_svc.poll(body.poll_token)
        except PairingGone as e:
            return JSONResponse({"error": "gone", "message": str(e)}, status_code=410)
        except DeviceLimit as e:
            # Approved, but the user reached the device limit meanwhile; the request stays collectable.
            return JSONResponse({"error": "limit", "message": str(e)}, status_code=403)
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

    async def finish_and_notify(call: OneWayCall) -> None:
        record = await call.finish()
        if notifier is None:
            return
        try:
            await notifier.call_finished(record)
        except Exception as e:  # noqa: BLE001 - the call is already in the history; a push failure only logs
            log.warning("call %s: push failed: %s", record.id, type(e).__name__)

    async def one_way_call(ws: WebSocket, device_id: str, agent: Agent, providers: OneWayProviders) -> None:
        """Records until hang-up (or the limit); transcription and delivery go on after the WebSocket closes."""
        # Before the record exists: a VAD that fails to load must not leave a call stuck in "recording".
        vad = build_vad(agent.spec.vad)
        call_log = await history.start(agent, device_id)
        record = call_log.record
        call = OneWayCall(
            agent, providers, vad, call_log,
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
            after_calls.spawn(finish_and_notify(call))
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
        vad = build_vad(agent.spec.vad)
        call_log = await history.start(agent, principal.device.id)
        record = call_log.record
        session = CallSession(
            agent.spec, provider_set, vad, transport, turn_end=turn_end, record=call_log.add,
        )
        warming: asyncio.Task[None] | None = None

        async def on_audio(data: bytes) -> bool:
            await session.on_audio(data)
            return False

        # From here on the record exists: whatever happens, it gets closed.
        try:
            await transport.send_json(
                protocol.session_ready(
                    secrets.token_hex(8), agent_summary(agent), turn_end,
                    protocol.AudioFormat(sample_rate=provider_set.tts.sample_rate), call_id=record.id,
                )
            )
            log.info(
                "call %s started: device=%s agent=%s (%s) turn_end=%s",
                record.id, principal.device.id, agent.id, agent.slug, turn_end,
            )
            warming = asyncio.create_task(warm_all(call_targets)) if call_targets else None
            await pump(ws, transport, on_audio, session.on_mute)
        finally:
            if warming is not None and not warming.done():
                warming.cancel()
            await session.close()
            ended = time.time()
            await call_log.save(status="ended" if call_log.count else "empty", ended_at=ended, finished=True)
            with suppress(Exception):
                await ws.close(code=protocol.CLOSE_NORMAL)
            log.info("call %s ended", record.id)

    if config.server.cors_origins:
        # Bearer tokens in a header, no cookies: no credentials. The outermost layer, so errors answered by the
        # routes (401, 404, 422) carry the headers too; a 500 is answered outside every middleware and does not.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
            expose_headers=["Content-Disposition"],
            max_age=600,
        )
    return app
