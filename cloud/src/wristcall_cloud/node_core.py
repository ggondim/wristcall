"""What the node routes share, free of routes (a route module imports this, never the other way round): the public
shape of an agent, the account's agent list and the month's usage."""

from typing import Any

from pymongo import ASCENDING

from .cloud_store import CloudModeStore, usage_month
from .config import CloudConfig


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
