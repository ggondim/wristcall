"""What the node routes share, free of routes (a route module imports this, never the other way round): the public
shape of an agent, the account's agent list, the month's usage and the closing of a call."""

import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .cloud_store import CloudModeStore, usage_month
from .config import CloudConfig

log = logging.getLogger("wristcall_cloud.node_core")

COUNT_TRIES = 5  # how often `_count` looks again after another closer changed the usage document first
FINAL_STATUSES = ("delivered", "failed", "empty", "ended")  # nothing more happens to a call that has one


def public_agent(doc: dict[str, Any]) -> dict[str, Any]:
    """An agent as the API shows it (the account that owns it stays out)."""
    return {
        "id": doc["_id"],
        "slug": doc.get("slug"),
        "display_name": doc.get("display_name"),
        "icon": doc.get("icon"),
        "color": doc.get("color"),
        "call_type": doc.get("call_type"),
        "position": doc.get("position"),
        "spec": doc.get("spec"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def public_call(doc: dict[str, Any]) -> dict[str, Any]:
    """A call as the API shows it (the account, node and grant bookkeeping stay out)."""
    return {
        "id": doc["_id"],
        "agent_id": doc.get("agent"),
        "agent_slug": doc.get("agent_slug"),
        "agent_name": doc.get("agent_name"),
        "device_id": doc.get("device"),
        "call_type": doc.get("call_type"),
        "status": doc.get("status"),
        "error": doc.get("error"),
        "attempts": doc.get("attempts"),
        "last_http_status": doc.get("last_http_status"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "ended_at": doc.get("ended_at"),
        "finished_at": doc.get("finished_at"),
        "expires_at": doc.get("expires_at"),
        "seconds": doc.get("seconds"),
        "open": bool(doc.get("open")),
    }


def public_entry(doc: dict[str, Any]) -> dict[str, Any]:
    """An entry of a call as the API shows it (`text` is sealed when `sealed`: the Cloud never opens it)."""
    return {
        "seq": doc["seq"],
        "role": doc.get("role"),
        "text": doc.get("text"),
        "sealed": doc.get("sealed", False),
        "error": doc.get("error"),
        "at": doc.get("at"),
    }


async def list_agents(cloud: CloudModeStore, account: str) -> list[dict[str, Any]]:
    """The account's agents (public form) by `position`, then `created_at`."""
    cursor = cloud.agents.find({"account": account}).sort([("position", ASCENDING), ("created_at", ASCENDING)])
    return [public_agent(doc) for doc in await cursor.to_list(None)]


async def month_usage(cloud: CloudModeStore, config: CloudConfig, account: str, now: float) -> dict[str, Any]:
    """The account's call seconds in the month `now` falls in, against the monthly quota (zero without a document).
    The limits are whole seconds: what is left is rounded down."""
    month = usage_month(now)
    doc = await cloud.usage.find_one({"_id": f"{account}:{month}"})
    used = doc.get("total_s", 0) if doc else 0
    quota = config.quota_minutes * 60
    return {"month": month, "used_s": used, "quota_s": quota, "remaining_s": max(0, math.floor(quota - used))}


async def _count(cloud: CloudModeStore, call: dict[str, Any], seconds: int) -> tuple[dict[str, Any] | None, bool]:
    """Makes the month's usage hold `seconds` for `call`, however often it is asked: the usage document remembers what
    it took from each call (`by_call`) and the update that adds the difference only matches while that is what it
    remembers. Returns the usage document as it is now (None when the call never counted) and whether this changed it."""
    usage_id = f"{call['account']}:{usage_month(call['created_at'])}"
    field = f"by_call.{call['_id']}"
    for _ in range(COUNT_TRIES):
        doc = await cloud.usage.find_one({"_id": usage_id})
        counted = ((doc or {}).get("by_call") or {}).get(call["_id"])
        if counted == seconds or (counted is None and not seconds):
            return doc, False
        delta = seconds - (counted or 0)
        try:
            updated = await cloud.usage.find_one_and_update(
                {"_id": usage_id, field: {"$exists": False} if counted is None else counted},
                {
                    "$setOnInsert": {"account": call["account"], "month": usage_month(call["created_at"])},
                    "$set": {field: seconds},
                    "$inc": {f"seconds_by_type.{call['call_type']}": delta, "total_s": delta},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:  # the document is there and another closer changed it first: look again
            continue
        return updated, True
    raise RuntimeError("usage kept changing under a call")


async def _settle(cloud: CloudModeStore, call_id: str) -> tuple[dict[str, Any] | None, bool]:
    """Counts the call's stored `seconds` into the usage; the call document is the truth, so when it changed
    meanwhile (a late `finish` correcting it) the count follows. The usage document and whether anything moved."""
    call = await cloud.calls.find_one({"_id": call_id})
    usage, changed = None, False
    while call is not None:
        usage, moved = await _count(cloud, call, call.get("seconds") or 0)
        changed = changed or moved
        latest = await cloud.calls.find_one({"_id": call_id})
        if latest is None or (latest.get("seconds") or 0) == (call.get("seconds") or 0):
            break
        call = latest
    return usage, changed


async def _free_account(cloud: CloudModeStore, call_id: str) -> bool:
    """Frees the account's open slot once the call's seconds are in the usage."""
    freed = await cloud.calls.update_one({"_id": call_id, "open": True}, {"$unset": {"open": ""}})
    return bool(freed.modified_count)


async def _warn(
    cloud: CloudModeStore,
    config: CloudConfig,
    call: dict[str, Any],
    usage: dict[str, Any] | None,
    now: float,
    on_quota_warning: Callable[[str, dict[str, Any]], Awaitable[Any]] | None,
) -> None:
    """The 80% warning, once per month: the one closer whose update sets `warned_80` calls `on_quota_warning`. A
    failing callback is logged, never raised: the call is closed whatever happens to the notification."""
    if on_quota_warning is None or usage is None:
        return
    used, quota = usage.get("total_s", 0), config.quota_minutes * 60
    if used * 5 < quota * 4 or "warned_80" in usage:
        return
    won = await cloud.usage.update_one(
        {"_id": usage["_id"], "warned_80": {"$exists": False}}, {"$set": {"warned_80": now}}
    )
    if not won.modified_count:
        return
    try:
        await on_quota_warning(call["account"], {"month": usage["month"], "used_s": used, "quota_s": quota})
    except Exception as e:
        log.warning("quota warning failed (%s)", type(e).__name__)


async def close_call(
    cloud: CloudModeStore,
    config: CloudConfig,
    call: dict[str, Any],
    *,
    status: str,
    error: str | None,
    seconds: float,
    closed_by: str,
    now: float,
    on_quota_warning: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
) -> bool:
    """Closes `call` (a document as stored) and adds its seconds to the month's usage, once, whoever asks and however
    often. True when this call changed something, False when it was already closed by whoever came first.

    Three steps, in this order, so that neither a crash nor an `open` call arriving in between loses or hides seconds:
    the first closer to find the call unclaimed writes its status and seconds (`closed_by` marks the claim, the call
    stays `open`); the seconds go into the usage (idempotent, see `_count`); only then the account's open slot is
    freed. A closer that finds a claimed call still open (the claimer stopped midway) completes the last two steps
    with what the claimer wrote, which is how a retried `finish` after a crash counts once.

    `closed_by="node"` is the node's `finish`: it also corrects a call the Cloud closed on its own
    (`closed_by="cloud"`, an orphan) when `finish` arrives late, taking the node's status, error and seconds and
    moving the usage by the difference. `closed_by="cloud"` (heartbeat, sweeper) only closes a call nobody closed.
    The usage month is the one the call began in. `seconds` is whole seconds: rounded up, and kept within what the
    call could have lasted (`max_s` plus a minute). After counting, `on_quota_warning(account, usage)` is called
    once per month, the first time the usage reaches 80% of the quota."""
    seconds = math.ceil(min(max(seconds, 0), (call.get("max_s") or 0) + 60))
    final = status in FINAL_STATUSES
    closing: dict[str, Any] = {
        "status": status, "error": error, "seconds": seconds, "ended_at": now, "updated_at": now, "closed_by": closed_by
    }
    if final:
        closing["finished_at"] = now
    claimed = await cloud.calls.update_one(
        {"_id": call["_id"], "open": True, "closed_by": {"$exists": False}}, {"$set": closing}
    )
    if claimed.modified_count:
        usage, _ = await _settle(cloud, call["_id"])
        await _free_account(cloud, call["_id"])
        await _warn(cloud, config, call, usage, now, on_quota_warning)
        return True
    current = await cloud.calls.find_one({"_id": call["_id"]})
    if current is None:
        return False
    changed, usage = False, None
    if current.get("open") and current.get("closed_by"):
        # Claimed by a closer that stopped before it freed the account: complete its last two steps.
        usage, counted = await _settle(cloud, call["_id"])
        changed = await _free_account(cloud, call["_id"]) or counted
    if closed_by == "node":
        # Closed by the Cloud before this `finish` arrived (T14): the node knows better.
        del closing["ended_at"]
        closing["finished_at"] = now if final else None
        swapped = await cloud.calls.update_one(
            {"_id": call["_id"], "closed_by": "cloud", "open": {"$exists": False}}, {"$set": closing}
        )
        if swapped.modified_count:
            usage, _ = await _settle(cloud, call["_id"])
            changed = True
    if changed:
        await _warn(cloud, config, call, usage, now, on_quota_warning)
    return changed
