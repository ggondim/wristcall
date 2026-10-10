"""Push relay keys (E6): a paired device or a management app (API token) registers the key the Cloud relay gave it,
and PushNotifier asks the relay to notify them.

The key is a secret: it is never logged and never sent back. Without the `push` config the routes answer 404.
"""

import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from .agents import ONE_WAY
from .auth import Authenticator
from .config import AppConfig, PushConfig
from .storage import CallRecord, Storage

log = logging.getLogger("wristcall.push")

PUSH_KEY = re.compile(r"^wc_push_[A-Za-z0-9_-]{43}$")
USER_AGENT = f"wristcall-server/{__version__}"
# What the Cloud takes: longer titles or bodies are refused, and so are control characters.
MAX_TITLE = 100
MAX_BODY = 300
MAX_NAME = 64


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


def _name(value: str, fallback: str) -> str:
    """A name fit for a notification text: no control characters, at most MAX_NAME characters."""
    clean = "".join(c for c in value if unicodedata.category(c) not in ("Cc", "Cs")).strip()
    if len(clean) > MAX_NAME:
        clean = clean[: MAX_NAME - 1].rstrip() + "\u2026"
    return clean or fallback


def _call_text(record: CallRecord) -> tuple[str, str] | None:
    agent = _name(record.agent_name, "your agent")
    if record.status == "delivered":
        return "Delivered", f"{agent} got your message."
    if record.status == "empty":
        return "Nothing heard", f"Nothing was sent to {agent}."
    if record.status != "failed":
        return None  # not finished
    if record.error == "delivery_failed":
        return "Not delivered", f"{agent} did not get your message. It is saved in the history."
    if record.error == "stt_failed":
        return "Not transcribed", f"Your message to {agent} could not be transcribed."
    return "Call failed", f"Your call to {agent} did not finish."


class PushNotifier:
    """Sends through the Cloud relay. Best effort: a relay that is down, slow or refusing only shows in the log
    (status code or error type, never the key nor the relay's answer). A key the relay says is gone is forgotten."""

    def __init__(self, storage: Storage, config: PushConfig, http: httpx.AsyncClient) -> None:
        self._storage = storage
        self._config = config
        self._http = http

    async def _request(self, method: str, path: str, push_key: str, body: dict[str, Any] | None = None) -> httpx.Response:
        # The httpx timeout bounds each step; the outer one bounds the whole exchange.
        async with asyncio.timeout(self._config.timeout_s):
            return await self._http.request(
                method,
                f"{self._config.relay_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {push_key}", "User-Agent": USER_AGENT},
                timeout=self._config.timeout_s,
                follow_redirects=False,
            )

    async def send(
        self,
        push_key: str,
        event: str,
        title: str,
        body: str,
        data: dict[str, Any],
        *,
        collapse_id: str | None = None,
    ) -> bool:
        message: dict[str, Any] = {"event": event, "title": title[:MAX_TITLE], "body": body[:MAX_BODY], "data": data}
        if collapse_id is not None:
            message["collapse_id"] = collapse_id
        try:
            r = await self._request("POST", "/v1/push/send", push_key, message)
        except Exception as e:  # noqa: BLE001 - only the type: the message may hold the URL or worse
            log.warning("push %s not sent: %s", event, type(e).__name__)
            return False
        if r.status_code == 202:
            return True
        if r.status_code == 410:
            try:
                await self._storage.push.forget(push_key)
                log.info("push %s not sent: the relay no longer knows the key, forgotten", event)
            except Exception as e:  # noqa: BLE001
                log.warning("forgetting a gone push key failed: %s", type(e).__name__)
            return False
        log.warning("push %s not sent: relay answered %d", event, r.status_code)
        return False

    async def call_finished(self, record: CallRecord) -> None:
        """Tells the device that made a one-way call how it ended."""
        if record.call_type not in ONE_WAY or record.device_id is None:
            return
        text = _call_text(record)
        if text is None:
            return
        key = await self._storage.push.for_device(record.device_id)
        if key is None:
            return
        data = {"call_id": record.id, "status": record.status, "error": record.error, "agent_id": record.agent_id}
        await self.send(key, "call.finished", *text, data, collapse_id=record.id)

    async def approval_requested(self, user_id: str, request_id: str, device_name: str, expires_at: float) -> None:
        """Tells the user's management apps (API tokens) that a device waits for their approval."""
        keys = await self._storage.push.for_apps(user_id)
        if not keys:
            return
        body = f'"{_name(device_name, "watch")}" wants to use your account. Open the app to approve it.'
        data = {"request_id": request_id, "device_name": device_name, "expires_at": expires_at}
        await asyncio.gather(*(self.send(key, "device.approval", "New device", body, data) for key in keys))

    async def discard(self, push_key: str) -> None:
        """Asks the relay to forget a key this server no longer uses."""
        try:
            r = await self._request("DELETE", "/v1/push/registrations/current", push_key)
        except Exception as e:  # noqa: BLE001
            log.warning("push key not discarded at the relay: %s", type(e).__name__)
            return
        if r.status_code not in (204, 404):
            log.warning("push key not discarded at the relay: relay answered %d", r.status_code)
