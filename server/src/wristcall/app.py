"""HTTP app: pairing (REST), calls (WebSocket /v1/call) and their status. Protocol in docs/protocol.md."""

import asyncio
import json
import logging
import re
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
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__, protocol
from .account import AccountError, AccountService
from .admission import CallSlots, Slot, call_kind
from .agents import (
    ONE_WAY, Agent, AgentError, AgentService, OneWayProviders, agent_summary, build_agent_providers,
    build_one_way_providers,
)
from .api import management_router
from .auth import Authenticator, Principal
from .bootstrap import bootstrap, log_report
from .cloud_node.calls import CloudPushNotifier
from .cloud_node.node import CloudNode
from .cloud_node.proof import ProofMismatch, require_proof
from .config import AppConfig
from .delivery import DeliveryPolicy
from .history import History, check_key
from .history_api import calls_router
from .history_codec import HistoryCodec
from .link import LinkService, canonical_public_url, public_url_in_audience, startup_invite
from .netguard import Resolver, public_http_client, system_resolver
from .oneway import Background, OneWayCall
from .pairing import DeviceLimit, Paired, PairingDenied, PairingGone, PairingService, Pending, clean_device_name
from .providers import ProviderError, check_providers
from .push import PushNotifier, push_router
from .ratelimit import RateLimiter
from .redelivery import Redelivery
from .session import CallSession
from .storage import (
    CallOpen, CloudUnavailable, DeviceRevoked, NotSupported, PlanRequired, Storage, open_sqlite_storage,
)
from .users import UserService
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


# Cloud mode: the routes of a server with its own users and devices. The Cloud keeps accounts and devices, so a
# cloud node answers them 404 `not_in_cloud_mode` (and redelivery until it goes through the Cloud, Task 8b).
_NOT_IN_CLOUD_MODE = re.compile(
    r"^/v1/(?:pair(?:/.*)?|pairing-codes(?:/.*)?|pairing-requests(?:/.*)?|account/link|devices(?:/.*)?"
    r"|calls/[^/]+/redeliver)/?$"
)
_CLOUD_ERRORS: list[tuple[type[Exception], int, str, str]] = [
    (CloudUnavailable, 503, "cloud_unavailable", "Wristcall Cloud is unavailable; try again."),
    (PlanRequired, 402, "plan_required", "This account has no Wristcall Cloud plan."),
    (DeviceRevoked, 403, "device_revoked", "This device was removed from the account."),
    (NotSupported, 404, "not_in_cloud_mode", "Not available on a Wristcall Cloud node."),
    (CallOpen, 409, "call_open", "This call is still open."),
    # Only the storage adapter's own check (no proof, or one for another account): never any PermissionError.
    (ProofMismatch, 401, "unauthorized", "missing or invalid token"),
]


def _not_in_cloud_mode() -> JSONResponse:
    return JSONResponse({"error": "not_in_cloud_mode", "message": "Not available on a Wristcall Cloud node."},
                        status_code=404)


class _NotInCloudMode:
    """Pure ASGI: answers the routes a cloud node does not have, and passes everything else on untouched. Unlike
    BaseHTTPMiddleware it never moves the endpoint to another task, so the request's account proof (a ContextVar
    set by the authenticator inside the endpoint) stays where the storage adapter reads it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (
            _NOT_IN_CLOUD_MODE.fullmatch(scope["path"])
            or (scope["method"] == "DELETE" and scope["path"].rstrip("/") == "/v1/me")
        ):
            await _not_in_cloud_mode()(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _add_cloud_error_handlers(app: FastAPI) -> None:
    for error, status, code, message in _CLOUD_ERRORS:
        def handler(_request: Request, _e: Exception, status: int = status, code: str = code, message: str = message
                    ) -> JSONResponse:
            return JSONResponse({"error": code, "message": message}, status_code=status)

        app.add_exception_handler(error, handler)


def _check_audience(config: AppConfig) -> None:
    """Not an error (the server may have another public name), but apps that reach it at `public_url` would
    ask the Cloud for tokens this server refuses."""
    if not public_url_in_audience(config):
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
    resolver: Resolver | None = None,
    cloud_http: httpx.AsyncClient | None = None,
    cloud_stop_s: float = 20.0,
    background_grace_s: float = 8.0,
    outbox_max_delay_s: float = 60.0,
) -> FastAPI:
    """Cloud mode (`config.is_cloud`) assembles a `CloudNode` (`app.state.cloud_node`; None self-hosted) whose
    storage and authenticator replace the SQLite ones; `storage` is then ignored. `cloud_http` reaches the Cloud
    (created and closed here when not given). `cloud_stop_s` is the outbox's time to empty at shutdown,
    `background_grace_s` the work after calls', `outbox_max_delay_s` the outbox's longest retry wait (tests shorten
    them)."""
    own_http = http is None
    http_client = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    check_providers(config, http_client)
    # The users' own endpoints: with limits.public_only, a client that checks every address it connects to.
    resolver = resolver or system_resolver
    own_user_http = config.limits.public_only
    user_http = (
        public_http_client(resolver, timeout=httpx.Timeout(30.0, connect=5.0)) if own_user_http else http_client
    )
    slots = CallSlots(config.limits.max_concurrent_calls)
    node: CloudNode | None = None
    own_cloud_http = cloud_http is None
    if config.is_cloud:
        cloud_http = cloud_http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        node = CloudNode.build(config, slots=slots, http=cloud_http, outbox_max_delay_s=outbox_max_delay_s)
        store: Storage = node.storage  # type: ignore[assignment] - the same interface, over the Cloud's API
    else:
        store = storage or open_sqlite_storage(config.server.data_dir)
    # Cloud mode: no local users, devices nor pairing (those routes answer 404, see _NotInCloudMode).
    pairing_svc = PairingService(
        store, config.server.pairing_approval, max_devices_per_user=config.limits.max_devices_per_user
    )
    agents = AgentService(
        store, config, http_client, resolver=resolver, plan_check=node.check_agent_plan if node else None
    )
    auth: Authenticator = node.auth if node else Authenticator(store)  # type: ignore[assignment]
    limiter = RateLimiter(limit=10, window_s=60)
    linked = config.central_account is not None and node is None
    account = AccountService(store, config.central_account, http_client) if linked else None
    links = LinkService(store, canonical_public_url(config)) if linked else None
    if linked:
        _check_audience(config)
    history = History(store, HistoryCodec(config.history.key()), config.history, account_scoped=node is not None)
    targets = warmup_targets(config, http_client)
    call_targets = [t for t in targets if t.config.on_call]
    after_calls = Background(grace_s=background_grace_s)
    # Pushes run with the work after the calls: the shutdown gives them the same few seconds, then cancels them.
    notifier = PushNotifier(store, config.push, http_client) if config.push else None
    # How a one-way call's device hears how it ended: the relay self-hosted, the Cloud's queue in cloud mode.
    call_notifier: PushNotifier | CloudPushNotifier | None = (
        CloudPushNotifier(node.outbox, node.registry) if node else notifier
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        purging: asyncio.Task[None] | None = None
        if node is not None:
            # Cloud mode: no key, users, retention nor leftover calls to look after here (the Cloud keeps the data,
            # moves its expiry and fails the calls an older boot left); the node starts beating and its outbox.
            await node.start()
        else:
            await check_key(store, history.codec)
            log_report(await bootstrap(store, config))
            try:
                if invite := await startup_invite(store, config, UserService(store), links):
                    logging.getLogger("wristcall.link").info("%s", invite)
            except Exception:  # the QR is a convenience: the server starts without it
                log.warning("could not prepare the link QR code; run `wristcall link` for one", exc_info=True)
            await history.apply_retention()
            purging = asyncio.create_task(purge_forever(history, config.history.purge_every_s))
            # Only the server does this (the CLI may run next to a live server): its own calls died with it.
            if interrupted := await store.calls.interrupt_unfinished(time.time()):
                log.warning("%d call(s) left unfinished by the last run marked as interrupted", interrupted)
        background = asyncio.create_task(run_background(targets)) if targets else None
        yield
        if purging is not None:
            purging.cancel()
            with suppress(asyncio.CancelledError):
                await purging
        await after_calls.close()
        if background is not None:
            background.cancel()
            with suppress(asyncio.CancelledError):
                await background
        if node is not None:
            # After the work after calls: what it queued (final status, push) gets the outbox's time to leave.
            await node.stop(cloud_stop_s)
            if own_cloud_http and cloud_http is not None:
                await cloud_http.aclose()
        if own_user_http:
            await user_http.aclose()
        if own_http:
            await http_client.aclose()

    app = FastAPI(title="wristcall", version=__version__, lifespan=lifespan)
    app.state.storage = store
    app.state.pairing = pairing_svc
    app.state.agents = agents
    app.state.auth = auth
    app.state.account = account
    app.state.history = history
    app.state.call_slots = slots
    app.state.cloud_node = node

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

    if node is None:
        redelivery = Redelivery(
            history, agents, config, http_client, user_http=user_http, policy=delivery_policy, background=after_calls
        )
        app.include_router(calls_router(auth, agents, history, redelivery.start))
    else:  # redelivery through the Cloud comes in Task 8b; until then its route answers not_in_cloud_mode
        app.include_router(calls_router(auth, agents, history, None))

    async def device_from(authorization: str | None) -> Principal | None:
        principal = await auth.authenticate(authorization)
        return principal if principal is not None and principal.kind == "device" else None

    @app.get("/v1/health")
    async def health() -> Any:
        central = config.central_account
        body = {
            "status": "ok",
            "version": __version__,
            "protocol": protocol.PROTOCOL_VERSION,
            "account": {"issuer": central.issuer, "device_credential": central.device_credential} if central else None,
            "push": {"relay": config.push.relay_url} if config.push else None,
        }
        if node is None:
            return body
        body["mode"] = "cloud"
        # Not ready before the Cloud accepted a heartbeat, nor while draining: the Cloud routes no calls here then.
        if node.draining or not node.ready.is_set():
            body["status"] = "draining" if node.draining else "starting"
            return JSONResponse(body, status_code=503)
        return body

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
            # The device carries the user's link: removing it (or linking another account) revokes the device.
            result = await pairing_svc.pair_for_user(
                user.id, body.device_name, approval=approval, central_subject=user.central_subject
            )
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

    async def cloud_me(cloud: CloudNode, authorization: str | None) -> Any:
        # Any platform: the watch and the apps alike. The Cloud says who the account is and what it may do.
        principal = await cloud.auth.authenticate(authorization)
        if principal is None:
            return JSONResponse({"error": "unauthorized", "message": "missing or invalid token"}, status_code=401)
        proof = require_proof(principal.user_id)
        found = await cloud.me(proof)
        listed = [Agent.from_record(r) for r in found["agents"]]
        return {
            "device_id": proof.device,
            "device_name": proof.platform,
            "user": {"id": proof.account, "handle": proof.account, "display_name": ""},
            "agents": [agent_summary(a) for a in listed],
            "profiles": [{"name": a.slug, "display_name": a.display_name} for a in listed],
            "plan": found["plan"],  # the Cloud's {"plan", "source", "expires_at"}, or null without a plan
            "usage": found["usage"],  # {"month", "used_s", "quota_s", "remaining_s"}
        }

    @app.get("/v1/me")
    async def me(authorization: str | None = Header(default=None)) -> Any:
        if node is not None:
            return await cloud_me(node, authorization)
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
            await ws.send_text(json.dumps(protocol.error(err.code, err.message, True, err.reason), ensure_ascii=False))
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

    async def finish_and_notify(call: OneWayCall, slot: Slot) -> None:
        try:
            record = await call.finish()
        finally:
            # Transcribed and delivered: the call stops counting against the limit (the push needs no GPU).
            slot.release()
        if call_notifier is None:
            return
        try:
            await call_notifier.call_finished(record)
        except Exception as e:  # noqa: BLE001 - the call is already in the history; a push failure only logs
            log.warning("call %s: push failed: %s", record.id, type(e).__name__)

    async def one_way_call(
        ws: WebSocket, device_id: str, agent: Agent, providers: OneWayProviders, slot: Slot,
    ) -> None:
        """Records until hang-up (or the limit); transcription and delivery go on after the WebSocket closes."""
        # The slot goes with the transcription once it is spawned; any way out before that gives it back here.
        handed_over = False
        try:
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
                after_calls.spawn(finish_and_notify(call, slot))
                handed_over = True
                with suppress(Exception):
                    await ws.close(code=protocol.CLOSE_NORMAL)
                log.info("call %s recorded", record.id)
        finally:
            if not handed_over:
                slot.release()

    @app.websocket("/v1/call")
    async def call(ws: WebSocket) -> None:
        await ws.accept()
        if node is not None:  # calls through the Cloud come in Task 9; until then a cloud node takes none
            await ws.close(code=protocol.CLOSE_UNAUTHORIZED)
            return
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
                    one_way = build_one_way_providers(config, agent.spec, http_client, user_http=user_http)
                else:
                    provider_set = build_agent_providers(config, agent.spec, http_client, user_http=user_http)
            except ProviderError as e:
                log.warning("agent %s unavailable: %s", agent.id, e)
                raise protocol.ProtocolError(
                    protocol.ErrorCode.AGENT_UNAVAILABLE, "this agent is not available; check its providers"
                ) from None
            # Before the call is recorded: an address that turned private since the agent was saved (or was saved
            # before public_only) fails now, before the user speaks. Every connection is checked again later.
            try:
                await agents.check_public(agent, include_stored_tts=False)
            except AgentError as e:
                log.warning("agent %s unavailable: %s", agent.id, e.message)
                raise protocol.ProtocolError(
                    protocol.ErrorCode.AGENT_UNAVAILABLE, "This agent uses an address this server cannot call.",
                    reason="url_not_public",
                ) from None
            # Last check before the call is recorded: a refused call leaves no trace in the history.
            kind = call_kind(agent.call_type)
            slot = slots.try_acquire(kind)
            if slot is None:
                log.warning("call refused: %s calls at the limit", kind)
                raise protocol.ProtocolError(protocol.ErrorCode.BUSY, "This server is busy; try again in a minute.")
        except protocol.ProtocolError as e:
            await fatal(ws, e)
            return

        if agent.call_type in ONE_WAY:
            await one_way_call(ws, principal.device.id, agent, one_way, slot)
            return

        # Clients that name the agent get its mode; 0.2.x clients (profile or nothing) keep 0.2.0's default,
        # because watch 0.1.0 omits turn_end when the user picks auto.
        turn_end = start.turn_end or (agent.spec.turn_end if start.agent else "auto")
        transport = _WsTransport(ws)
        # Holds a slot from here until the WebSocket is closed, whatever happens.
        try:
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
                # Before the close: once the client sees it closed, its next call must find the slot free.
                slot.release()
                with suppress(Exception):
                    await ws.close(code=protocol.CLOSE_NORMAL)
                log.info("call %s ended", record.id)
        finally:
            # First, with no await before it: under cancellation the first await would skip the rest.
            slot.release()

    if node is not None:
        _add_cloud_error_handlers(app)
        # Inside CORS (added after it would be outside): the 404s carry its headers too.
        app.add_middleware(_NotInCloudMode)

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
