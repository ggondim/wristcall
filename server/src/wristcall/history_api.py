"""History API (design decisions 13 and 16): list and search, read, delete, export and redeliver calls.

Reading one call (`GET /v1/calls/{id}`) takes a device or a user API token, as in 0.4.0: the watch asks how its call
went. Everything else takes the user's API token only (the iPhone app and the PWA): a lost watch does not hand out the
whole history.
"""

import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .agents import AgentError, AgentService
from .auth import Authenticator, Principal
from .history import History
from .history_export import TimeError, export_json, export_markdown, parse_time
from .storage import CallRecord

MAX_PAGE = 100
AGENT_ID = re.compile(r"^ag_[0-9a-f]{12}$")

# Starts the redelivery of a failed one-way call; returns the call as it is now, or raises RedeliveryError.
Redeliver = Callable[[CallRecord], Awaitable[CallRecord]]


class RedeliveryError(Exception):
    """code: not_failed | no_text | busy | agent_gone | not_one_way | agent_unavailable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def resolve_agent_id(agents: AgentService, user_id: str, ref: str) -> str | None:
    """An agent's id by id or slug; a deleted agent's id still names its calls. None if neither."""
    try:
        return (await agents.get(user_id, ref)).id
    except AgentError:
        return ref if AGENT_ID.fullmatch(ref) else None


def cursor(record: CallRecord) -> str:
    """`next_before`: where the next page starts (a position, valid even if that call is deleted meanwhile)."""
    return f"{record.created_at!r}:{record.id}"


def parse_cursor(value: str) -> tuple[float, str] | None:
    at, sep, call_id = value.partition(":")
    try:
        position = float(at)
    except ValueError:
        return None
    if not sep or not call_id or not math.isfinite(position):
        return None
    return position, call_id


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def calls_router(
    auth: Authenticator,
    agents: AgentService,
    history: History,
    redeliver: Redeliver | None = None,
    *,
    now: Callable[[], float] = time.time,
) -> APIRouter:
    router = APIRouter(prefix="/v1/calls")
    calls = history.storage.calls

    async def owner(authorization: str | None) -> Principal | JSONResponse:
        who = await auth.authenticate(authorization)
        if who is None:
            return _error("unauthorized", "missing or invalid token", 401)
        if who.kind != "api":
            return _error("forbidden", "this needs a user API token, not a device token", 403)
        return who

    async def agent_id(user_id: str, ref: str) -> str | JSONResponse:
        found = await resolve_agent_id(agents, user_id, ref)
        return found if found is not None else _error("not_found", f"agent not found: {ref}", 404)

    def period(since: str | None, until: str | None) -> tuple[float | None, float | None] | JSONResponse:
        try:
            return (
                parse_time(since) if since is not None else None,
                parse_time(until) if until is not None else None,
            )
        except TimeError as e:
            return _error("invalid", str(e), 422)

    async def filters(
        who: Principal, agent: str | None, since: str | None, until: str | None
    ) -> dict[str, Any] | JSONResponse:
        found = await agent_id(who.user_id, agent) if agent is not None else None
        if isinstance(found, JSONResponse):
            return found
        span = period(since, until)
        if isinstance(span, JSONResponse):
            return span
        return {"agent_id": found, "since": span[0], "until": span[1]}

    @router.get("")
    async def list_calls(
        authorization: str | None = Header(default=None),
        agent: str | None = None,
        q: str | None = Query(default=None, max_length=500),
        since: str | None = None,
        until: str | None = None,
        before: str | None = None,
        limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    ) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        where = await filters(who, agent, since, until)
        if isinstance(where, JSONResponse):
            return where
        position = parse_cursor(before) if before is not None else None
        if before is not None and position is None:
            return _error("invalid", "before: pass the next_before of the previous page", 422)
        terms = history.codec.query_terms(q) if q and q.strip() else None
        page = await calls.list(who.user_id, **where, terms=terms, before=position, limit=limit)
        return {
            "calls": [await history.detail(r) for r in page],
            # Ask again with before=next_before for the next page; null when this was the last one.
            "next_before": cursor(page[-1]) if len(page) == limit else None,
        }

    @router.delete("")
    async def delete_calls(
        authorization: str | None = Header(default=None), agent: str | None = None, all: bool = False,
    ) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        if (agent is None) == (not all):
            return _error("invalid", "send either agent=<id or slug> or all=true", 422)
        found = await agent_id(who.user_id, agent) if agent is not None else None
        if isinstance(found, JSONResponse):
            return found
        return {"deleted": await calls.delete_all(who.user_id, found)}

    @router.get("/export")
    async def export_calls(
        authorization: str | None = Header(default=None),
        format: str = "md",
        agent: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        if format not in ("md", "json"):
            return _error("invalid", "format: md or json", 422)
        where = await filters(who, agent, since, until)
        if isinstance(where, JSONResponse):
            return where

        moment = now()
        stamp = datetime.fromtimestamp(moment, UTC).strftime("%Y%m%d-%H%M%S")
        views = history.details(who.user_id, **where)
        body = export_markdown(views, moment) if format == "md" else export_json(views, moment)
        media = "text/markdown; charset=utf-8" if format == "md" else "application/json"
        return StreamingResponse(
            body, media_type=media,
            headers={"Content-Disposition": f'attachment; filename="wristcall-history-{stamp}.{format}"'},
        )

    @router.get("/{call_id}")
    async def get_call(call_id: str, authorization: str | None = Header(default=None)) -> Any:
        # Device or API token: a user sees only their own calls.
        who = await auth.authenticate(authorization)
        if who is None:
            return _error("unauthorized", "missing or invalid token", 401)
        record = await calls.get(who.user_id, call_id)
        if record is None:
            return _error("not_found", "call not found", 404)
        return await history.detail(record)

    @router.delete("/{call_id}")
    async def delete_call(call_id: str, authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        if not await calls.delete(who.user_id, call_id):
            return _error("not_found", "call not found", 404)
        return Response(status_code=204)

    @router.post("/{call_id}/redeliver")
    async def redeliver_call(call_id: str, authorization: str | None = Header(default=None)) -> Any:
        who = await owner(authorization)
        if isinstance(who, JSONResponse):
            return who
        record = await calls.get(who.user_id, call_id)
        if record is None:
            return _error("not_found", "call not found", 404)
        if redeliver is None:
            return _error("not_found", "redelivery is not available", 404)
        try:
            started = await redeliver(record)
        except RedeliveryError as e:
            return _error(e.code, e.message, 409)
        return JSONResponse(await history.detail(started), status_code=202)

    return router
