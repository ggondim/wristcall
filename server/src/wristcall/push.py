"""Push relay keys (E6): a paired device or a management app (API token) registers the key the Cloud relay gave it.

The key is a secret: it is never logged and never sent back. Without the `push` config the routes answer 404.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from .auth import Authenticator
from .config import AppConfig
from .storage import Storage

log = logging.getLogger("wristcall.push")

PUSH_KEY = re.compile(r"^wc_push_[A-Za-z0-9_-]{43}$")


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def push_router(
    storage: Storage,
    auth: Authenticator,
    config: AppConfig,
    *,
    on_discarded: Callable[[str], Awaitable[None]] | None = None,
    now: Callable[[], float] = time.time,
) -> APIRouter:
    """`on_discarded(key)` runs in the background, after the response, for a key that was replaced or deleted
    (the relay should forget it). Its failures are logged without the key."""
    router = APIRouter(prefix="/v1")
    pending: set[asyncio.Task[None]] = set()

    def discard(key: str | None) -> None:
        if key is None or on_discarded is None:
            return
        task = asyncio.get_running_loop().create_task(_run(on_discarded, key))
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def _run(hook: Callable[[str], Awaitable[None]], key: str) -> None:
        try:
            await hook(key)
        except Exception as e:  # noqa: BLE001 - a relay failure must not surface; only the type is logged
            log.warning("discarding a push key failed: %s", type(e).__name__)

    def not_configured() -> JSONResponse:
        return _error("not_configured", "this server has no push relay configured", 404)

    @router.put("/push")
    async def put_push(request: Request, authorization: str | None = Header(default=None)) -> Any:
        if config.push is None:
            return not_configured()
        who = await auth.authenticate(authorization)
        if who is None:
            return _error("unauthorized", "missing or invalid token", 401)
        try:
            body = await request.json()
        except (ValueError, RecursionError):
            return _error("invalid", "body must be valid JSON", 422)
        key = body.get("push_key") if isinstance(body, dict) else None
        # The body is never echoed: it holds the key.
        if not isinstance(key, str) or not PUSH_KEY.fullmatch(key):
            return _error("invalid", "push_key must be a wc_push_ key", 422)
        try:
            if who.kind == "device":
                replaced = await storage.push.set(who.user_id, key, now(), device_id=who.device.id)
            else:
                replaced = await storage.push.set(who.user_id, key, now(), token_id=who.token_id)
        except KeyError:  # the client stopped being valid between the authentication and now
            return _error("unauthorized", "missing or invalid token", 401)
        discard(replaced)
        return Response(status_code=204)

    @router.delete("/push")
    async def delete_push(authorization: str | None = Header(default=None)) -> Any:
        if config.push is None:
            return not_configured()
        who = await auth.authenticate(authorization)
        if who is None:
            return _error("unauthorized", "missing or invalid token", 401)
        if who.kind == "device":
            deleted = await storage.push.clear(device_id=who.device.id)
        else:
            deleted = await storage.push.clear(token_id=who.token_id)
        if deleted is None:
            return _error("not_found", "no push key registered", 404)
        discard(deleted)
        return Response(status_code=204)

    return router
