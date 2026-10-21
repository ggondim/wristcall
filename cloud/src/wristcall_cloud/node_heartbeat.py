"""`POST /v1/node/heartbeat` and the orphan calls: a node tells the Cloud it is alive and which calls it holds; calls
the Cloud has open for it that it does not hold are closed (`close_call`, `closed_by="cloud"`), and a periodic
sweeper does the same for a node that went silent.

A restarted Cloud has no heartbeats to show for the time it was down, so the sweeper counts a node's silence from the
later of its last heartbeat and the Cloud's own start (`state.started_at`). The `processing` calls of a node that came
back with another `boot_id`, or went silent, are failed (`interrupted`): nothing is processing them any more. Those
are updated in place, without `closed_by`: only a late `PATCH` of the node can still correct them (a `finish` finds
the call already closed and changes nothing). Errors never echo
what the node sent.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pymongo import ReturnDocument
from starlette.datastructures import State

from .api_common import ApiError, json_object
from .cloud_store import ONE_WAY, CloudModeStore
from .node_auth import current_node
from .node_calls import _integer
from .node_core import close_call
from .node_push import send_quota_warning

log = logging.getLogger("wristcall_cloud.node_heartbeat")

router = APIRouter(prefix="/v1/node")

MAX_ACTIVE_CALLS = 500
MAX_ID_LENGTH = 64
MAX_BOOT_ID = 64
MAX_VERSION = 32
MAX_COUNT = 1_000_000
KINDS = ("conversation", "one_way")


def _invalid(message: str) -> ApiError:
    return ApiError("invalid", message, 422)


def _text(value: Any, name: str, longest: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= longest:
        raise _invalid(f"{name} must be a string of 1 to {longest} characters")
    return value


def _counts(value: Any, name: str, *, nullable: bool) -> dict[str, int | None]:
    """`{conversation, one_way}` of whole numbers from 0 (or null, when `nullable`: no limit)."""
    if not isinstance(value, dict) or any(kind not in value for kind in KINDS):
        raise _invalid(f"{name} must have {' and '.join(KINDS)}")
    out: dict[str, int | None] = {}
    for kind in KINDS:
        count = value[kind]
        if not (_integer(count, 0, MAX_COUNT) or (nullable and count is None)):
            raise _invalid(f"{name} values must be whole numbers from 0" + (" or null" if nullable else ""))
        out[kind] = count
    return out


def parse_heartbeat(body: dict[str, Any]) -> dict[str, Any]:
    active = body.get("active_call_ids")
    if not isinstance(active, list) or len(active) > MAX_ACTIVE_CALLS:
        raise _invalid(f"active_call_ids must list at most {MAX_ACTIVE_CALLS} ids")
    providers_ok = body.get("providers_ok")
    if not isinstance(providers_ok, bool):
        raise _invalid("providers_ok must be true or false")
    return {
        "boot_id": _text(body.get("boot_id"), "boot_id", MAX_BOOT_ID),
        "version": _text(body.get("version"), "version", MAX_VERSION),
        "active_call_ids": [_text(call_id, "active_call_ids items", MAX_ID_LENGTH) for call_id in active],
        "active_by_type": _counts(body.get("active_by_type"), "active_by_type", nullable=False),
        "capacity": _counts(body.get("capacity"), "capacity", nullable=True),
        "providers_ok": providers_ok,
    }


async def _close_orphan(state: State, call: dict[str, Any], seconds: float, now: float) -> bool:
    """Closes an open call nobody is holding: a one-way call fails, a conversation ends, both `interrupted`."""
    return await close_call(
        state.cloud,
        state.config,
        call,
        status="failed" if call["call_type"] in ONE_WAY else "ended",
        error="interrupted",
        seconds=seconds,
        closed_by="cloud",
        now=now,
        on_quota_warning=partial(send_quota_warning, state),
    )


async def _fail_processing(cloud: CloudModeStore, node_id: str, now: float) -> int:
    """The calls of `node_id` closed in `processing` become `failed`/`interrupted` (T17). `closed_by` stays as it is:
    a `PATCH` that arrives later can still correct the call (a `finish` cannot)."""
    changed = await cloud.calls.update_many(
        {"node": node_id, "status": "processing", "open": {"$exists": False}},
        {"$set": {"status": "failed", "error": "interrupted", "finished_at": now, "updated_at": now}},
    )
    return changed.modified_count


@router.post("/heartbeat", status_code=204)
async def heartbeat(request: Request, node: dict[str, Any] = Depends(current_node)) -> Response:
    beat = parse_heartbeat(await json_object(request))
    state: State = request.app.state
    cloud: CloudModeStore = state.cloud
    now = time.time()
    # One update keeps the previous `last_seen_at` as `prev_seen_at` and hands back what was there before.
    fields = {
        key: {"$literal": beat[key]} for key in ("boot_id", "version", "active_by_type", "capacity", "providers_ok")
    }
    previous = await cloud.nodes.find_one_and_update(
        {"_id": node["_id"]},
        [{"$set": {"prev_seen_at": "$last_seen_at", "last_seen_at": {"$literal": now}, **fields}}],
        projection={"last_seen_at": 1, "boot_id": 1},
        return_document=ReturnDocument.BEFORE,
    )
    if previous is None:  # the node was deleted since its credential was read
        raise ApiError("unauthorized", "missing or invalid node credential", 401)
    old_boot = previous.get("boot_id")
    held = beat["active_call_ids"]
    orphans = cloud.calls.find(
        {
            "node": node["_id"],
            "open": True,
            "_id": {"$nin": held},
            "created_at": {"$lt": now - state.config.orphan_grace_s},
        }
    )
    async for call in orphans:
        # Until the heartbeat before this one: the last time the node was known to hold the call.
        await _close_orphan(state, call, (previous.get("last_seen_at") or now) - call["created_at"], now)
    if old_boot is not None and old_boot != beat["boot_id"]:
        # After the orphans, as the sweeper does: a call a crashed `finish` left open as `processing` is freed above
        # and failed here.
        await _fail_processing(cloud, node["_id"], now)
    return Response(status_code=204)


async def sweep_orphans(state: State, *, now: float) -> int:
    """Closes the open calls of the nodes that went silent (nothing heard for `node_stale_s`, counted from the later
    of their last heartbeat and the start of this Cloud) and fails their `processing` calls. Returns how many calls
    changed. An open call is taken whatever `closed_by` says: a closer may have claimed it and stopped."""
    cloud: CloudModeStore = state.cloud
    node_ids = set(await cloud.calls.distinct("node", {"open": True}))
    node_ids |= set(await cloud.calls.distinct("node", {"status": "processing", "open": {"$exists": False}}))
    changed = 0
    for node_id in sorted(node_ids):
        node = await cloud.nodes.find_one({"_id": node_id}, projection={"last_seen_at": 1})
        seen = (node or {}).get("last_seen_at")
        if now - max(seen or 0, state.started_at) <= state.config.node_stale_s:
            continue
        async for call in cloud.calls.find({"node": node_id, "open": True}):
            # Until the last heartbeat; a node never heard from held it for no time at all.
            if await _close_orphan(state, call, (seen or call["created_at"]) - call["created_at"], now):
                changed += 1
        changed += await _fail_processing(cloud, node_id, now)
    return changed


async def orphan_sweep_loop(
    state: State,
    *,
    every_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.time,
) -> None:
    """Every `every_s`, runs `sweep_orphans`. Runs until cancelled; a failed round is logged and the next one tries
    again."""
    while True:
        await sleep(every_s)
        try:
            closed = await sweep_orphans(state, now=now())
        except Exception as e:  # whatever went wrong, the next round tries again
            log.warning("sweeping orphan calls failed (%s)", type(e).__name__)
            continue
        if closed:
            log.info("changed %d orphan calls", closed)
