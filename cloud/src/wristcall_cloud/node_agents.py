"""`/v1/node/agents*`: the account's agents in cloud mode, as a node reads and writes them for an app.

Reading is open to every device of the account (a watch lists and calls); writing needs a management platform
(`require_manager`) and a valid plan, except `DELETE`: a lapsed plan must not keep sealed secrets from being removed.
The Cloud reads only the public fields and `spec.retention_days`; the rest of `spec` (endpoints sealed by the node)
is stored as sent. Errors never echo what the caller sent.
"""

import hashlib
import json
import re
import time
import unicodedata
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pymongo import ASCENDING, DESCENDING, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from .agenda import COLORS
from .api_common import ApiError, bson_safe, json_object
from .cloud_store import CALL_TYPES, CloudModeStore
from .config import CloudConfig
from .node_auth import NodeCaller, node_caller, require_manager
from .node_core import list_agents, public_agent

router = APIRouter(prefix="/v1/node")

AGENT_ID = re.compile(r"^ag_[0-9a-f]{12}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ICON = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)*$")
MAX_ICON = 64
MAX_NAME = 64
# Compact JSON: the server's worst case is a 20,000 character `system_prompt` plus endpoints sealed in base64.
MAX_SPEC_BYTES = 256 * 1024
MAX_SPEC_DEPTH = 32  # BSON stops at 100 levels; stay well under it instead of answering 500
MAX_RETENTION_DAYS = 36500
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1  # BSON holds no integer outside this range

_NEW_FIELDS = {"id", "slug", "display_name", "icon", "color", "call_type", "spec"}
_REQUIRED = _NEW_FIELDS - {"color"}
_PATCH_FIELDS = _NEW_FIELDS - {"id"}


def _invalid(message: str) -> ApiError:
    return ApiError("invalid", message, 422)


def _not_found() -> ApiError:
    return ApiError("not_found", "agent not found", 404)


def _conflict() -> ApiError:
    return ApiError("conflict", "an agent with this id or slug already exists", 409)


def _text(value: Any, field: str, pattern: re.Pattern[str], limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or not pattern.fullmatch(value):
        raise _invalid(f"{field} is not valid")
    return value


def _display_name(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME:
        raise _invalid(f"display_name must be a string of 1 to {MAX_NAME} characters")
    # `Cs` is a lone surrogate: not text BSON can hold.
    if not bson_safe(value) or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value):
        raise _invalid("display_name must not contain control characters")
    return value


def _walk(value: Any, depth: int) -> None:
    """Rejects what BSON cannot store: nesting past `MAX_SPEC_DEPTH`, a NUL in a key, a lone surrogate in a key or a
    string, an integer outside int64."""
    if depth > MAX_SPEC_DEPTH:
        raise _invalid("spec is nested too deeply")
    if isinstance(value, dict):
        for key, item in value.items():
            if "\x00" in key or not bson_safe(key):
                raise _invalid("spec has a key that is not allowed")
            _walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _walk(item, depth + 1)
    elif isinstance(value, str):
        if not bson_safe(value):
            raise _invalid("spec has text that is not valid")
    elif isinstance(value, int) and not INT64_MIN <= value <= INT64_MAX:
        raise _invalid("spec has a number out of range")


def _spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid("spec must be an object")
    _walk(value, 1)
    try:
        size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, RecursionError):
        raise _invalid("spec is not valid") from None
    if size > MAX_SPEC_BYTES:
        raise _invalid(f"spec must be at most {MAX_SPEC_BYTES} bytes")
    days = value.get("retention_days")
    if days is not None and days != "forever":
        # A bool is an int in Python; neither it nor a float is a number of days.
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_RETENTION_DAYS:
            raise _invalid(f"spec.retention_days must be 1 to {MAX_RETENTION_DAYS}, \"forever\" or null")
    return value


def _fields(body: dict[str, Any]) -> dict[str, Any]:
    """Validates each field present in `body` (which fields may appear is the callers' business)."""
    out: dict[str, Any] = {}
    if "id" in body:
        if not isinstance(body["id"], str) or not AGENT_ID.fullmatch(body["id"]):
            raise _invalid("id must look like ag_ and 12 hex characters")
        out["id"] = body["id"]
    if "slug" in body:
        out["slug"] = _text(body["slug"], "slug", SLUG, 32)
    if "display_name" in body:
        out["display_name"] = _display_name(body["display_name"])
    if "icon" in body:
        out["icon"] = _text(body["icon"], "icon", ICON, MAX_ICON)
    if "color" in body:
        if body["color"] is not None and body["color"] not in COLORS:
            raise _invalid(f"color must be one of {', '.join(COLORS)} or null")
        out["color"] = body["color"]
    if "call_type" in body:
        if body["call_type"] not in CALL_TYPES:
            raise _invalid(f"call_type must be one of {', '.join(CALL_TYPES)}")
        out["call_type"] = body["call_type"]
    if "spec" in body:
        out["spec"] = _spec(body["spec"])
    return out


def parse_new_agent(body: dict[str, Any]) -> dict[str, Any]:
    """The validated fields of a new agent (`color` defaults to None); 422 `invalid` for anything else."""
    if set(body) - _NEW_FIELDS:
        raise _invalid("agent has fields that are not allowed")
    if _REQUIRED - set(body):
        raise _invalid("agent is missing fields")
    return {"color": None, **_fields(body)}


def parse_agent_patch(body: dict[str, Any]) -> dict[str, Any]:
    """The validated fields a patch changes (at least one; the id never changes); 422 `invalid` otherwise."""
    if set(body) - _PATCH_FIELDS:
        raise _invalid("patch has fields that are not allowed")
    if not body:
        raise _invalid("patch must change at least one field")
    return _fields(body)


def agents_etag(agents: list[dict[str, Any]]) -> str:
    """Changes whenever an agent is added, removed, edited or moved (`updated_at` and `position` tell)."""
    keys = [[a["id"], a["updated_at"], a["position"]] for a in agents]
    digest = hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'


async def set_agent_expiry(cloud: CloudModeStore, account: str, agent_id: str, days: int) -> int:
    """Points the expiry of the agent's calls and entries at `days` after their call began; returns how many changed.

    Entries carry `call_created_at` since they do not know their own start. `purge_at` is the BSON date of
    `expires_at`, computed in the pipeline so both fields cannot drift apart.
    """

    def stages(created_field: str) -> list[dict[str, Any]]:
        expires = {"$add": [f"${created_field}", days * 86400]}
        return [{"$set": {"expires_at": expires, "purge_at": {"$toDate": {"$multiply": [expires, 1000]}}}}]

    scope = {"account": account, "agent": agent_id}
    calls = await cloud.calls.update_many(scope, stages("created_at"))
    entries = await cloud.entries.update_many(scope, stages("call_created_at"))
    return calls.modified_count + entries.modified_count


async def _require_plan(request: Request, caller: NodeCaller) -> None:
    if await request.app.state.accounts.entitlement(caller.account, now=time.time()) is None:
        raise ApiError("plan_required", "this needs an active plan", 402)


def _listing(agents: list[dict[str, Any]]) -> JSONResponse:
    etag = agents_etag(agents)
    return JSONResponse({"agents": agents, "etag": etag}, headers={"ETag": etag})


def _matches(header: str | None, etag: str) -> bool:
    return header is not None and any(
        part.strip().removeprefix("W/") in (etag, "*") for part in header.split(",")
    )


@router.get("/agents")
async def get_agents(request: Request, caller: NodeCaller = Depends(node_caller)) -> Response:
    agents = await list_agents(request.app.state.cloud, caller.account)
    etag = agents_etag(agents)
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag})
    return _listing(agents)


@router.put("/agents/order")
async def order_agents(request: Request, caller: NodeCaller = Depends(node_caller)) -> Response:
    require_manager(caller)
    await _require_plan(request, caller)
    body = await json_object(request)
    agent, index = body.get("agent"), body.get("index")
    if set(body) != {"agent", "index"} or not isinstance(agent, str):
        raise _invalid("send agent and index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise _invalid("index must be an integer")
    cloud: CloudModeStore = request.app.state.cloud
    cursor = cloud.agents.find({"account": caller.account}).sort([("position", ASCENDING), ("created_at", ASCENDING)])
    docs = await cursor.to_list(None)
    ids = [d["_id"] for d in docs]
    if agent not in ids:
        raise _not_found()
    ids.remove(agent)
    ids.insert(min(max(index, 0), len(ids)), agent)
    current = {d["_id"]: d.get("position") for d in docs}
    moves = [
        UpdateOne({"_id": agent_id, "account": caller.account}, {"$set": {"position": position}})
        for position, agent_id in enumerate(ids)
        if current[agent_id] != position
    ]
    if moves:
        await cloud.agents.bulk_write(moves, ordered=False)
    return _listing(await list_agents(cloud, caller.account))


@router.get("/agents/{ref}")
async def get_agent(ref: str, request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, Any]:
    doc = await request.app.state.cloud.agents.find_one(
        {"account": caller.account, "$or": [{"_id": ref}, {"slug": ref}]}
    )
    if doc is None:
        raise _not_found()
    return public_agent(doc)


@router.post("/agents", status_code=201)
async def create_agent(request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, Any]:
    require_manager(caller)
    await _require_plan(request, caller)
    fields = parse_new_agent(await json_object(request))
    cloud: CloudModeStore = request.app.state.cloud
    config: CloudConfig = request.app.state.config
    limit_reached = ApiError("limit", f"at most {config.max_cloud_agents} agents per account", 403)
    # A cheap check first; it cannot hold the cap alone (requests that all pass it would all insert), so the insert
    # is checked after, below.
    if await cloud.agents.count_documents({"account": caller.account}) >= config.max_cloud_agents:
        raise limit_reached
    last = await cloud.agents.find_one({"account": caller.account}, sort=[("position", DESCENDING)])
    now = time.time()
    doc = {
        **{k: v for k, v in fields.items() if k != "id"},
        "_id": fields["id"],
        "account": caller.account,
        "position": 0 if last is None else (last.get("position") or 0) + 1,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await cloud.agents.insert_one(doc)
    except DuplicateKeyError:
        raise _conflict() from None
    # Insert, then count: of requests racing past the check above, each sees at least the agents inserted before its
    # own, so no more than the cap survive (a request may be refused that would have fitted, never the reverse).
    # Known limits: this may over-refuse in a race, and a fault between `insert_one` and `delete_one` can leave the
    # account above the cap until an agent is deleted.
    if await cloud.agents.count_documents({"account": caller.account}) > config.max_cloud_agents:
        await cloud.agents.delete_one({"_id": doc["_id"], "account": caller.account})
        raise limit_reached
    return public_agent(doc)


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, Any]:
    require_manager(caller)
    await _require_plan(request, caller)
    fields = parse_agent_patch(await json_object(request))
    cloud: CloudModeStore = request.app.state.cloud
    config: CloudConfig = request.app.state.config
    changes = {**fields, "updated_at": time.time()}
    try:
        before = await cloud.agents.find_one_and_update(
            {"_id": agent_id, "account": caller.account}, {"$set": changes}, return_document=ReturnDocument.BEFORE
        )
    except DuplicateKeyError:
        raise _conflict() from None
    if before is None:
        raise _not_found()
    after = {**before, **changes}
    if "spec" in fields:
        old_days = (before.get("spec") or {}).get("retention_days")
        new_days = fields["spec"].get("retention_days")
        if old_days != new_days:
            await set_agent_expiry(cloud, caller.account, agent_id, config.retention_days(new_days))
    return public_agent(after)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, request: Request, caller: NodeCaller = Depends(node_caller)) -> Response:
    require_manager(caller)
    result = await request.app.state.cloud.agents.delete_one({"_id": agent_id, "account": caller.account})
    if result.deleted_count == 0:
        raise _not_found()
    return Response(status_code=204)
