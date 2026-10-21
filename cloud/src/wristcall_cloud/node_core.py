"""What the node routes share, free of routes (a route module imports this, never the other way round): the public
shape of an agent, the account's agent list, the month's usage and the closing of a call."""

from collections.abc import Awaitable, Callable
from typing import Any

from pymongo import ASCENDING, ReturnDocument

from .cloud_store import CloudModeStore, usage_month
from .config import CloudConfig

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


async def list_agents(cloud: CloudModeStore, account: str) -> list[dict[str, Any]]:
    """The account's agents (public form) by `position`, then `created_at`."""
    cursor = cloud.agents.find({"account": account}).sort([("position", ASCENDING), ("created_at", ASCENDING)])
    return [public_agent(doc) for doc in await cursor.to_list(None)]


async def month_usage(cloud: CloudModeStore, config: CloudConfig, account: str, now: float) -> dict[str, Any]:
    """The account's call seconds in the month `now` falls in, against the monthly quota (zero without a document)."""
    month = usage_month(now)
    doc = await cloud.usage.find_one({"_id": f"{account}:{month}"})
    used = doc.get("total_s", 0) if doc else 0
    quota = config.quota_minutes * 60
    return {"month": month, "used_s": used, "quota_s": quota, "remaining_s": max(0, quota - used)}


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

    `closed_by="node"` is the node's `finish`: it closes a call that is open, or corrects one the Cloud closed on its
    own (`closed_by="cloud"`, an orphan) when `finish` arrives late, taking the node's status, error and seconds and
    adding only the difference to the usage. `closed_by="cloud"` (heartbeat, sweeper) only closes an open call.
    Each step is a conditional update, so two closers racing never count a call twice. The usage month is the one the
    call began in. `seconds` is kept within what the call could have lasted (`max_s` plus a minute).
    `on_quota_warning` is accepted for the 80% warning and not used yet."""
    seconds = min(max(seconds, 0), (call.get("max_s") or 0) + 60)
    final = status in FINAL_STATUSES
    closing: dict[str, Any] = {
        "status": status, "error": error, "seconds": seconds, "ended_at": now, "updated_at": now, "closed_by": closed_by
    }
    if final:
        closing["finished_at"] = now
    closed = await cloud.calls.update_one(
        {"_id": call["_id"], "open": True}, {"$set": closing, "$unset": {"open": ""}}
    )
    if closed.modified_count:
        delta = seconds
    elif closed_by == "node":
        # Closed by the Cloud before this `finish` arrived (T14): the node knows better.
        del closing["ended_at"]
        closing["finished_at"] = now if final else None
        before = await cloud.calls.find_one_and_update(
            {"_id": call["_id"], "closed_by": "cloud"}, {"$set": closing}, return_document=ReturnDocument.BEFORE
        )
        if before is None:
            return False
        delta = seconds - (before.get("seconds") or 0)
    else:
        return False
    if delta:
        await cloud.usage.update_one(
            {"_id": f"{call['account']}:{usage_month(call['created_at'])}"},
            {
                "$setOnInsert": {"account": call["account"], "month": usage_month(call["created_at"])},
                "$inc": {f"seconds_by_type.{call['call_type']}": delta, "total_s": delta},
            },
            upsert=True,
        )
    return True
