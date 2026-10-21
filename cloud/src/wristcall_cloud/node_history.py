"""`/v1/node/calls*` history: list and search by term hash, read one call, export, delete, redeliver.

Listing, searching, exporting, deleting and redelivering need a management platform (`require_manager`); reading
one call is open to every device of the account, but a watch sees only its own calls. A call that belongs to another
account is a 404 like one that does not exist. Open calls are never deleted. Entries hold sealed text: the Cloud
searches by the hashes the node computed and never opens what it stores. Errors never echo what the caller sent.
"""

import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from .api_common import ApiError
from .cloud_store import ONE_WAY, CloudModeStore
from .config import CloudConfig
from .node_auth import NodeCaller, _signer, issue_grant, node_caller, require_manager
from .node_calls import TERM_HASH
from .node_core import public_agent, public_call, public_entry

router = APIRouter(prefix="/v1/node")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TERM_GROUPS = 16
MAX_HASHES_PER_GROUP = 16
MAX_REF = 64
REDELIVERABLE = ("delivery_failed", "interrupted")  # what a failed call may be redelivered after (T16)
DELETE_BATCH = 500


def _invalid(message: str) -> ApiError:
    return ApiError("invalid", message, 422)


def _not_found() -> ApiError:
    return ApiError("not_found", "call not found", 404)


def _call_open() -> ApiError:
    return ApiError("call_open", "this call is still open", 409)


def _single(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise _invalid(f"{name} must be given once")
    return values[0] if values else None


def _time(request: Request, name: str) -> float | None:
    raw = _single(request, name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise _invalid(f"{name} must be a number") from None
    if not math.isfinite(value):
        raise _invalid(f"{name} must be a number")
    return value


def _ref(request: Request, name: str) -> str | None:
    raw = _single(request, name)
    if raw is not None and not 0 < len(raw) <= MAX_REF:
        raise _invalid(f"{name} is not valid")
    return raw


def _limit(request: Request) -> int:
    raw = _single(request, "limit")
    if raw is None:
        return DEFAULT_LIMIT
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= MAX_LIMIT:
        raise _invalid(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return int(raw)


def _term_groups(request: Request) -> list[list[str]]:
    """Each `terms` is a group of hashes separated by commas; a call must match every group."""
    raws = request.query_params.getlist("terms")
    if len(raws) > MAX_TERM_GROUPS:
        raise _invalid(f"terms may be given at most {MAX_TERM_GROUPS} times")
    groups: list[list[str]] = []
    for raw in raws:
        hashes = raw.split(",")
        if len(hashes) > MAX_HASHES_PER_GROUP:
            raise _invalid(f"each terms group holds at most {MAX_HASHES_PER_GROUP} hashes")
        if not all(TERM_HASH.fullmatch(h) for h in hashes):
            raise _invalid("terms must be hashes of the form x and 16 hex digits")
        groups.append(list(dict.fromkeys(hashes)))
    return groups


def _range(request: Request) -> dict[str, Any]:
    """The `created_at` window: `since <= created_at < until`."""
    since, until = _time(request, "since"), _time(request, "until")
    window: dict[str, Any] = {}
    if since is not None:
        window["$gte"] = since
    if until is not None:
        window["$lt"] = until
    return {"created_at": window} if window else {}


async def _calls_with_terms(
    cloud: CloudModeStore, account: str, agent: str | None, groups: list[list[str]]
) -> list[str] | None:
    """The ids of the calls that have, for every group, an entry with one of its hashes (None: no search)."""
    if not groups:
        return None
    found: set[str] | None = None
    for group in groups:
        query: dict[str, Any] = {"account": account, "term_hashes": {"$in": group}}
        if agent is not None:
            query["agent"] = agent
        ids = set(await cloud.entries.distinct("call", query))
        found = ids if found is None else found & ids
        if not found:
            return []
    return sorted(found or ())


@router.get("/calls")
async def list_calls(request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, Any]:
    require_manager(caller)
    agent, limit, groups = _ref(request, "agent"), _limit(request), _term_groups(request)
    before_at, before_id = _time(request, "before_at"), _ref(request, "before_id")
    if (before_at is None) != (before_id is None):
        raise _invalid("before_at and before_id go together")
    cloud: CloudModeStore = request.app.state.cloud
    clauses: list[dict[str, Any]] = [{"account": caller.account}, _range(request)]
    if agent is not None:
        clauses.append({"agent": agent})
    if before_at is not None:
        clauses.append(
            {"$or": [{"created_at": {"$lt": before_at}}, {"created_at": before_at, "_id": {"$lt": before_id}}]}
        )
    ids = await _calls_with_terms(cloud, caller.account, agent, groups)
    if ids is not None:
        clauses.append({"_id": {"$in": ids}})
    cursor = cloud.calls.find({"$and": [c for c in clauses if c]}).sort(
        [("created_at", DESCENDING), ("_id", DESCENDING)]
    ).limit(limit)
    return {"calls": [public_call(doc) for doc in await cursor.to_list(limit)]}


async def _export_lines(cloud: CloudModeStore, query: dict[str, Any]) -> AsyncIterator[bytes]:
    """One NDJSON line per call, oldest first, then the end line; a stream cut short has no end line. Works only on
    what it was given (the store and the query), not on the request, which is gone when the body is sent."""
    count = 0
    calls = cloud.calls.find(query).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
    try:
        async for call in calls:
            entries = cloud.entries.find({"call": call["_id"]}).sort("seq", ASCENDING)
            try:
                listed = [public_entry(e) async for e in entries]
            finally:
                await entries.close()
            line = {"call": public_call(call), "entries": listed}
            yield (json.dumps(line, separators=(",", ":")) + "\n").encode()
            count += 1
        yield (json.dumps({"end": True, "count": count}, separators=(",", ":")) + "\n").encode()
    finally:
        await calls.close()


@router.get("/calls/export")
async def export_calls(request: Request, caller: NodeCaller = Depends(node_caller)) -> StreamingResponse:
    require_manager(caller)
    agent = _ref(request, "agent")
    query: dict[str, Any] = {"account": caller.account, **_range(request)}
    if agent is not None:
        query["agent"] = agent
    return StreamingResponse(_export_lines(request.app.state.cloud, query), media_type="application/x-ndjson")


@router.get("/calls/{call_id}")
async def read_call(call_id: str, request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, Any]:
    cloud: CloudModeStore = request.app.state.cloud
    call = await cloud.calls.find_one({"_id": call_id, "account": caller.account})
    if call is None or (caller.platform == "watchos" and call.get("device") != caller.device):
        raise _not_found()
    cursor = cloud.entries.find({"call": call_id}).sort("seq", ASCENDING)
    return {"call": public_call(call), "entries": [public_entry(e) for e in await cursor.to_list(None)]}


@router.delete("/calls/{call_id}", status_code=204)
async def delete_call(call_id: str, request: Request, caller: NodeCaller = Depends(node_caller)) -> Response:
    require_manager(caller)
    cloud: CloudModeStore = request.app.state.cloud
    call = await cloud.calls.find_one({"_id": call_id, "account": caller.account}, {"open": 1})
    if call is None:
        raise _not_found()
    if call.get("open"):
        raise _call_open()
    await cloud.calls.delete_one({"_id": call_id, "account": caller.account, "open": {"$ne": True}})
    await cloud.entries.delete_many({"call": call_id})
    return Response(status_code=204)


@router.delete("/calls")
async def delete_calls(request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, int]:
    """Deletes the account's calls (of one agent, or all); the open ones stay."""
    require_manager(caller)
    agent, everything = _ref(request, "agent"), _single(request, "all") == "true"
    if agent is None and not everything:
        raise _invalid("give agent or all=true")
    cloud: CloudModeStore = request.app.state.cloud
    query: dict[str, Any] = {"account": caller.account, "open": {"$ne": True}}
    if agent is not None:
        query["agent"] = agent
    deleted = 0
    cursor = cloud.calls.find(query, {"_id": 1})
    try:
        batch: list[str] = []
        async for doc in cursor:
            batch.append(doc["_id"])
            if len(batch) >= DELETE_BATCH:
                deleted += await _delete_batch(cloud, caller.account, batch)
                batch = []
        if batch:
            deleted += await _delete_batch(cloud, caller.account, batch)
    finally:
        await cursor.close()
    return {"deleted": deleted}


async def _delete_batch(cloud: CloudModeStore, account: str, ids: list[str]) -> int:
    result = await cloud.calls.delete_many({"_id": {"$in": ids}, "account": account, "open": {"$ne": True}})
    await cloud.entries.delete_many({"call": {"$in": ids}, "account": account})
    return result.deleted_count


@router.post("/calls/{call_id}/redeliver")
async def redeliver_call(call_id: str, request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, Any]:
    """Hands a failed one-way call to the node that asked, to deliver it again: same rules as the server's
    redelivery. The call becomes that node's (`processing`, `attempts` kept) and a new grant lets it report."""
    require_manager(caller)
    cloud: CloudModeStore = request.app.state.cloud
    config: CloudConfig = request.app.state.config
    call = await cloud.calls.find_one({"_id": call_id, "account": caller.account})
    if call is None:
        raise _not_found()
    if call.get("call_type") not in ONE_WAY:
        raise ApiError("not_one_way", "only one-shot and monologue calls are delivered", 409)
    if call.get("open"):
        raise _call_open()
    if call.get("status") == "processing":
        raise ApiError("busy", "this call is being delivered right now", 409)
    if call.get("status") != "failed" or call.get("error") not in REDELIVERABLE:
        raise ApiError("not_failed", "only a call whose delivery failed can be delivered again", 409)
    key, issuer = _signer(request)
    now = time.time()
    # Conditional on `failed`: of two requests racing, one wins and the other finds the call busy.
    updated = await cloud.calls.find_one_and_update(
        {"_id": call_id, "account": caller.account, "status": "failed", "open": {"$ne": True}},
        {"$set": {
            "status": "processing", "error": None, "finished_at": None, "node": caller.node["_id"], "updated_at": now,
            # A call the Cloud closed as an orphan comes back as the node's: a later `finish` must not pass the
            # swap filter (`closed_by: "cloud"`) and rewrite what was already counted.
            "closed_by": "node",
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise ApiError("busy", "this call is being delivered right now", 409)
    agent = await cloud.agents.find_one({"_id": updated["agent"], "account": caller.account})
    entries = await cloud.entries.find({"call": call_id}).sort("seq", ASCENDING).to_list(None)
    return {
        "call": public_call(updated),
        "entries": [public_entry(e) for e in entries],
        "agent": public_agent(agent) if agent is not None else None,
        "grant": issue_grant(
            key, issuer=issuer, caller=caller, call_id=call_id, max_s=config.max_one_way_s, now=now
        ),
    }
