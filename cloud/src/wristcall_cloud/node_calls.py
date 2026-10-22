"""`/v1/node/calls*`: a node opens a call for a device (plan, monthly quota, one open call per account), writes its
history entries and status, and closes it with `finish`, which adds its seconds to the month's usage.

Opening needs the node's credential and the user's token (`node_caller`); everything after it needs the credential and
the call grant issued on opening (`call_of_grant`), and works with an expired grant: the node's queue may deliver
late. Entries and status writes are idempotent. The Cloud never stores text in the clear (entries arrive `sealed`).
Errors never echo what the caller sent.
"""

import math
import re
import secrets
import time
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from .api_common import ApiError, bson_safe, json_object
from .cloud_store import CALL_STATUSES, CALL_TYPES, ONE_WAY, CloudModeStore, purge_at
from .config import CloudConfig
from .node_auth import NodeCaller, _signer, current_node, issue_grant, node_caller, verify_grant
from .node_core import close_call, month_usage, public_agent
from .node_push import send_quota_warning
from .sessions_api import entitlement_response

router = APIRouter(prefix="/v1/node")

TERM_HASH = re.compile(r"^x[0-9a-f]{16}$")
ERROR_CODE = re.compile(r"^[a-z_]{1,32}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_ENTRIES_PER_POST = 100
MAX_TERMS_PER_ENTRY = 2000
MAX_ENTRY_TEXT = 256 * 1024
MAX_SEQ = 100000
MAX_ATTEMPTS = 10000
MAX_NUMBER = 2**53  # the largest whole number a double holds exactly; a time or a number of seconds never gets near
ENTRY_ROLES = ("user", "agent")
DELIVERIES = ("webhook", "history")  # how the node delivered a call (the server's `CallRecord.delivery`)
FINISH_STATUSES = tuple(s for s in CALL_STATUSES if s != "recording")
_PATCH_FIELDS = {"status", "error", "attempts", "last_http_status", "ended_at", "finished_at", "delivery"}


def _invalid(message: str) -> ApiError:
    return ApiError("invalid", message, 422)


def _not_found() -> ApiError:
    return ApiError("not_found", "call not found", 404)


def _number(value: Any) -> bool:
    """A finite JSON number below `MAX_NUMBER` in absolute value (a bool is an int in Python, but not a number
    here). A huge integer cannot become a float (`OverflowError`), and past int64 BSON cannot hold it."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and abs(float(value)) < MAX_NUMBER
    except OverflowError:
        return False


def _integer(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _error_code(value: Any) -> str | None:
    if value is not None and not (isinstance(value, str) and ERROR_CODE.fullmatch(value)):
        raise _invalid("error must be null or a short code of lowercase letters and underscores")
    return value


def _entry(value: Any) -> dict[str, Any]:
    """One entry of a batch, checked and reduced to what is stored."""
    if not isinstance(value, dict):
        raise _invalid("entries must be objects")
    seq, role, text, at = value.get("seq"), value.get("role"), value.get("text"), value.get("at")
    sealed, hashes = value.get("sealed", False), value.get("term_hashes") or []
    if not _integer(seq, 0, MAX_SEQ):
        raise _invalid(f"seq must be an integer from 0 to {MAX_SEQ}")
    if role not in ENTRY_ROLES:
        raise _invalid(f"role must be one of {', '.join(ENTRY_ROLES)}")
    if not _number(at):
        raise _invalid("at must be a number")
    if not isinstance(sealed, bool):
        raise _invalid("sealed must be true or false")
    if text is not None:
        if not isinstance(text, str):
            raise _invalid("text must be a string or null")
        if not bson_safe(text):  # a lone surrogate: not text BSON can hold
            raise _invalid("text is not valid")
        if len(text.encode("utf-8")) > MAX_ENTRY_TEXT:
            raise _invalid(f"text must be at most {MAX_ENTRY_TEXT} bytes")
        if not sealed:
            raise _invalid("text must be sealed")
    if not isinstance(hashes, list) or len(hashes) > MAX_TERMS_PER_ENTRY:
        raise _invalid(f"term_hashes must be a list of at most {MAX_TERMS_PER_ENTRY} hashes")
    if not all(isinstance(h, str) and TERM_HASH.fullmatch(h) for h in hashes):
        raise _invalid("term_hashes must be hashes of the form x and 16 hex digits")
    return {
        "seq": seq, "role": role, "text": text, "sealed": sealed, "error": _error_code(value.get("error")),
        "at": at, "term_hashes": list(dict.fromkeys(hashes)),
    }


def parse_entries(body: dict[str, Any]) -> list[dict[str, Any]]:
    entries = body.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES_PER_POST:
        raise _invalid(f"entries must be a list of at most {MAX_ENTRIES_PER_POST} entries")
    parsed = [_entry(item) for item in entries]
    if len({e["seq"] for e in parsed}) != len(parsed):
        raise _invalid("entries must not repeat a seq")
    return parsed


def _delivery(value: Any) -> str | None:
    if value is not None and (not isinstance(value, str) or value not in DELIVERIES):
        raise _invalid(f"delivery must be null or one of {', '.join(DELIVERIES)}")
    return value


def parse_patch(body: dict[str, Any]) -> dict[str, Any]:
    if not body or not set(body) <= _PATCH_FIELDS:
        raise _invalid("patch must change at least one of " + ", ".join(sorted(_PATCH_FIELDS)))
    out: dict[str, Any] = {}
    if "status" in body:
        if body["status"] not in CALL_STATUSES:
            raise _invalid(f"status must be one of {', '.join(CALL_STATUSES)}")
        out["status"] = body["status"]
    if "error" in body:
        out["error"] = _error_code(body["error"])
    if "attempts" in body:
        if not _integer(body["attempts"], 0, MAX_ATTEMPTS):
            raise _invalid(f"attempts must be an integer from 0 to {MAX_ATTEMPTS}")
        out["attempts"] = body["attempts"]
    if "last_http_status" in body:
        if body["last_http_status"] is not None and not _integer(body["last_http_status"], 100, 599):
            raise _invalid("last_http_status must be null or an HTTP status")
        out["last_http_status"] = body["last_http_status"]
    if "delivery" in body:
        out["delivery"] = _delivery(body["delivery"])
    for field in ("ended_at", "finished_at"):
        if field in body:
            if body[field] is not None and not _number(body[field]):
                raise _invalid(f"{field} must be a number or null")
            out[field] = body[field]
    return out


def parse_finish(body: dict[str, Any]) -> tuple[str, str | None, float]:
    status, seconds = body.get("status"), body.get("seconds")
    if status not in FINISH_STATUSES:
        raise _invalid(f"status must be one of {', '.join(FINISH_STATUSES)}")
    if not _number(seconds):
        raise _invalid("seconds must be a number")
    return status, _error_code(body.get("error")), seconds


def _agent_retention_days(config: CloudConfig, agent: dict[str, Any]) -> int:
    return config.retention_days((agent.get("spec") or {}).get("retention_days"))


async def _open_response(
    request: Request, caller: NodeCaller, call: dict[str, Any], agent: dict[str, Any], entitlement: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    key, issuer = _signer(request)
    usage = await month_usage(request.app.state.cloud, request.app.state.config, caller.account, now)
    return {
        "call_id": call["_id"],
        "grant": issue_grant(key, issuer=issuer, caller=caller, call_id=call["_id"], max_s=call["max_s"], now=now),
        "agent": public_agent(agent),
        "plan": entitlement_response(entitlement),
        "quota_s": usage["remaining_s"],
        "max_s": call["max_s"],
        "started_at": call["created_at"],
    }


async def _agent_of(cloud: CloudModeStore, account: str, ref: str) -> dict[str, Any] | None:
    return await cloud.agents.find_one({"account": account, "$or": [{"_id": ref}, {"slug": ref}]})


async def _retry_of(cloud: CloudModeStore, caller: NodeCaller, request_id: str | None) -> dict[str, Any] | None:
    """The call this node already opened for this device with `request_id` and that is still open (the answer to
    opening it was lost)."""
    if request_id is None:
        return None
    return await cloud.calls.find_one(
        {"account": caller.account, "open": True, "request_id": request_id, "node": caller.node["_id"],
         "device": caller.device}
    )


@router.post("/calls", status_code=201)
async def open_call(
    request: Request, response: Response, caller: NodeCaller = Depends(node_caller)
) -> dict[str, Any]:
    body = await json_object(request)
    ref, call_type, request_id = body.get("agent"), body.get("call_type"), body.get("request_id")
    if not isinstance(ref, str) or not 0 < len(ref) <= 64 or not bson_safe(ref):
        raise _invalid("agent must be the id or slug of an agent")
    if call_type not in CALL_TYPES:
        raise _invalid(f"call_type must be one of {', '.join(CALL_TYPES)}")
    if request_id is not None and not (isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id)):
        raise _invalid("request_id must be 8 to 64 letters, digits, dashes or underscores")
    delivery = _delivery(body.get("delivery"))
    cloud: CloudModeStore = request.app.state.cloud
    config: CloudConfig = request.app.state.config
    now = time.time()
    entitlement = await request.app.state.accounts.entitlement(caller.account, now=now)
    if entitlement is None:
        raise ApiError("plan_required", "this needs an active plan", 402)

    async def again() -> dict[str, Any] | None:
        call = await _retry_of(cloud, caller, request_id)
        agent = await _agent_of(cloud, caller.account, call["agent"]) if call else None
        if call is None or agent is None:
            return None
        if call.get("call_type") != call_type or ref not in (agent["_id"], agent.get("slug")):
            raise ApiError("conflict", "request_id was used to open a different call", 409)
        response.status_code = 200
        return await _open_response(request, caller, call, agent, entitlement, now)

    if (replay := await again()) is not None:
        return replay
    agent = await _agent_of(cloud, caller.account, ref)
    if agent is None:
        raise ApiError("not_found", "agent not found", 404)
    if agent.get("call_type") != call_type:
        raise _invalid("call_type does not match the agent")
    usage = await month_usage(cloud, config, caller.account, now)
    if usage["remaining_s"] <= 0:
        raise ApiError("quota_exceeded", "this month's minutes are used up", 402)
    max_s = min(config.max_one_way_s if call_type in ONE_WAY else config.max_conversation_s, usage["remaining_s"])
    expires_at = now + _agent_retention_days(config, agent) * 86400
    call = {
        "_id": "c_" + secrets.token_hex(8),
        "account": caller.account,
        "device": caller.device,
        "platform": caller.platform,
        "node": caller.node["_id"],
        "agent": agent["_id"],
        "agent_slug": agent.get("slug"),
        "agent_name": agent.get("display_name"),
        "call_type": call_type,
        "status": "recording",
        "open": True,
        "request_id": request_id,
        "delivery": delivery,
        "created_at": now,
        "updated_at": now,
        "max_s": max_s,
        "expires_at": expires_at,
        "purge_at": purge_at(expires_at),
        "error": None,
        "attempts": 0,
        "last_http_status": None,
        "ended_at": None,
        "finished_at": None,
        "seconds": None,
    }
    try:
        await cloud.calls.insert_one(call)
    except DuplicateKeyError:
        # The unique index on open calls decides the race. A retry that lost it to its own first attempt still
        # gets that call.
        if (replay := await again()) is not None:
            return replay
        raise ApiError("too_many_calls", "this account already has a call open", 409) from None
    return await _open_response(request, caller, call, agent, entitlement, now)


async def call_of_grant(
    call_id: str,
    request: Request,
    node: dict[str, Any] = Depends(current_node),
    x_wristcall_call_grant: str | None = Header(default=None),
) -> dict[str, Any]:
    """The call a request works on: its grant must be this node's, for this call (an expired one still counts, within
    `verify_grant`'s window), and the call must be this account's and this node's (404 otherwise)."""
    grant = verify_grant(request, node, call_id, x_wristcall_call_grant, allow_expired=True)
    cloud: CloudModeStore = request.app.state.cloud
    call = await cloud.calls.find_one({"_id": call_id, "account": grant.account, "node": node["_id"]})
    if call is None:
        raise _not_found()
    return call


@router.post("/calls/{call_id}/entries", status_code=204)
async def add_entries(request: Request, call: dict[str, Any] = Depends(call_of_grant)) -> Response:
    entries = parse_entries(await json_object(request))
    if entries:
        # Per call, the date and the expiry come from the call; `$setOnInsert` makes a resent seq a no-op.
        inherited = {
            "account": call["account"], "call": call["_id"], "agent": call["agent"],
            "call_created_at": call["created_at"], "expires_at": call.get("expires_at"),
            "purge_at": call.get("purge_at"),
        }
        await request.app.state.cloud.entries.bulk_write(
            [
                UpdateOne({"_id": f"{call['_id']}:{e['seq']}"}, {"$setOnInsert": {**inherited, **e}}, upsert=True)
                for e in entries
            ],
            ordered=False,
        )
    return Response(status_code=204)


@router.patch("/calls/{call_id}", status_code=204)
async def patch_call(request: Request, call: dict[str, Any] = Depends(call_of_grant)) -> Response:
    changes = parse_patch(await json_object(request))
    changes["updated_at"] = time.time()
    await request.app.state.cloud.calls.update_one({"_id": call["_id"]}, {"$set": changes})
    return Response(status_code=204)


@router.post("/calls/{call_id}/finish", status_code=204)
async def finish_call(request: Request, call: dict[str, Any] = Depends(call_of_grant)) -> Response:
    status, error, seconds = parse_finish(await json_object(request))
    await close_call(
        request.app.state.cloud, request.app.state.config, call,
        status=status, error=error, seconds=seconds, closed_by="node", now=time.time(),
        on_quota_warning=partial(send_quota_warning, request.app.state),
    )
    return Response(status_code=204)
