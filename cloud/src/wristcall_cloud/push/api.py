"""Push relay routes. Registering is anonymous (limited per client address); the other routes take the push key as
`Authorization: Bearer wc_push_...`.

`410 gone` tells a server to forget a key for good, so it is only ever said about a key the database answered it does
not know: a database error is `503 push_unavailable`, never `410`.
"""

import logging
import math
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pymongo.errors import PyMongoError
from starlette.datastructures import State

from ..api_common import client_ip
from ..config import CloudConfig
from ..store import Store
from .channels import Channel, ChannelGone, ChannelUnavailable, Message
from .limits import RegistrationLimiter, SendLimiter
from .registry import (
    PLATFORMS,
    PushError,
    is_push_key,
    key_id,
    new_push_key,
    parse_message,
    parse_registration,
    public_registration,
)

log = logging.getLogger("wristcall_cloud.push")

STATUS = {
    "invalid": 422,
    "unauthorized": 401,
    "not_configured": 404,
    "not_found": 404,
    "gone": 410,
    "rate_limited": 429,
    "push_unavailable": 503,
}


def push_error_response(e: PushError) -> JSONResponse:
    headers = {"Retry-After": str(e.retry_after)} if e.retry_after is not None else None
    return JSONResponse({"error": e.code, "message": e.message}, status_code=STATUS[e.code], headers=headers)


def _unavailable() -> PushError:
    return PushError("push_unavailable", "push is unavailable; try again later")


class PushRoute(APIRoute):
    """Any MongoDB error in a push route is 503: never a 500, and never a 410 that would make a server drop a key."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except PyMongoError as e:
                log.warning("push storage unavailable (%s)", type(e).__name__)
                return push_error_response(_unavailable())

        return guarded


router = APIRouter(route_class=PushRoute)


async def _json(request: Request) -> Any:
    """The request body as parsed JSON (size already capped by BodyLimit); the parsers check its shape."""
    try:
        return await request.json()
    except (ValueError, RecursionError):
        raise PushError("invalid", "body must be valid JSON") from None


def _push_key(authorization: str | None) -> str | None:
    """The bearer value (401 without one); None when it is not shaped like a push key."""
    scheme, _, value = (authorization or "").partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        raise PushError("unauthorized", "missing push key")
    return value if is_push_key(value) else None


def _store(request: Request) -> Store:
    return request.app.state.store


def _retry_after(seconds: float) -> int:
    return max(1, math.ceil(seconds))


async def _gone_unless_known(request: Request, authorization: str | None) -> tuple[str, dict[str, Any]]:
    key = _push_key(authorization)
    registration = await _store(request).get_registration(key_id(key)) if key is not None else None
    if registration is None:
        raise PushError("gone", "this push key is not registered")
    return key_id(key), registration


@router.post("/v1/push/registrations", status_code=201)
async def register(request: Request) -> dict[str, Any]:
    config: CloudConfig = request.app.state.config
    limiter: RegistrationLimiter = request.app.state.registration_limiter
    wait = limiter.allow(client_ip(request, config.client_ip_header))
    if wait is not None:
        raise PushError("rate_limited", "too many registrations; try again later", retry_after=_retry_after(wait))
    body = await _json(request)
    channels: dict[str, Channel] = request.app.state.channels
    platform = body.get("platform") if isinstance(body, dict) else None
    if platform in PLATFORMS and platform not in channels:
        raise PushError("not_configured", f"this cloud does not relay {platform} notifications")
    fields = parse_registration(body, config)
    push_key = new_push_key()
    await _store(request).add_registration(key_id(push_key), fields)
    log.info("push registered (platform %s)", fields["platform"])
    return {"push_key": push_key, **public_registration(fields)}


@router.get("/v1/push/registrations/current")
async def current(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _, registration = await _gone_unless_known(request, authorization)
    return public_registration(registration)


@router.delete("/v1/push/registrations/current", status_code=204)
async def unregister(request: Request, authorization: str | None = Header(default=None)) -> Response:
    key = _push_key(authorization)
    if key is None or not await _store(request).delete_registration(key_id(key)):
        raise PushError("not_found", "this push key is not registered")
    log.info("push registration deleted")
    return Response(status_code=204)


async def _deliver(
    state: State, registration_id: str, registration: dict[str, Any], message: Message
) -> tuple[str, int | None]:
    """Sends `message` to the registration's device: the outcome (see `relay_to_registration`) and, when rate limited,
    the seconds to wait. The budget is checked before the channel is called."""
    limiter: SendLimiter = state.send_limiter
    wait = limiter.allow(registration_id)
    if wait is not None:
        return "rate_limited", _retry_after(wait)
    platform = registration["platform"]
    channel: Channel | None = state.channels.get(platform)
    if channel is None:
        # The registration was made while the channel existed: the key is still good.
        log.warning("push channel not configured (platform %s)", platform)
        return "unavailable", None
    store: Store = state.store
    await store.touch_registration(registration_id)
    try:
        await channel.send(registration, message)
    except ChannelGone:
        await store.delete_registration(registration_id)
        log.info("push channel gone, registration deleted (platform %s)", platform)
        return "gone", None
    except ChannelUnavailable:
        log.warning("push channel unavailable (platform %s)", platform)
        return "unavailable", None
    log.info("push sent (platform %s, event %s)", platform, message.event)
    return "sent", None


async def relay_to_registration(
    state: State, registration_id: str, registration: dict[str, Any], body: dict[str, Any]
) -> str:
    """Relays `body` (a message as `/v1/push/send` takes it) to one registration. The outcome: `"sent"`, `"gone"` (the
    channel says the device is gone; the registration is deleted), `"rate_limited"`, `"unavailable"` (channel down or
    not configured) or `"skipped"` (an event the registration did not ask for). A malformed body raises `PushError`
    `invalid`; a storage error propagates."""
    event = body.get("event")
    if isinstance(event, str) and event != "test" and event not in registration["events"]:
        # Not for this registration, but a malformed message is still invalid.
        parse_message(body, {**registration, "events": [*registration["events"], event]})
        return "skipped"
    outcome, _ = await _deliver(state, registration_id, registration, parse_message(body, registration))
    return outcome


@router.post("/v1/push/send", status_code=202)
async def send(request: Request, authorization: str | None = Header(default=None)) -> dict[str, str]:
    # Order matters: an unknown key is 410 whatever the body, and spends nobody's budget; an invalid message spends
    # nothing either (an event the registration did not ask for is invalid here, not skipped).
    registration_id, registration = await _gone_unless_known(request, authorization)
    message = parse_message(await _json(request), registration)
    outcome, wait = await _deliver(request.app.state, registration_id, registration, message)
    if outcome == "rate_limited":
        raise PushError("rate_limited", "too many notifications; try again later", retry_after=wait)
    if outcome == "unavailable":
        raise _unavailable()
    if outcome == "gone":
        raise PushError("gone", "this push key is not registered")
    return {"status": "sent"}
