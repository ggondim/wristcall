"""MongoDB collections of the cloud mode: nodes, their agents, calls, history entries, push targets, usage
and the nodes' hourly stats.

This module only owns the collection handles and their indexes. The Cloud never opens what a node sends (secrets,
history): it keeps opaque envelopes and hashes. Secrets (node credentials) are stored as SHA-256 hex digests only.
"""

from datetime import UTC, datetime

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

CALL_TYPES = ("conversation", "one-shot", "monologue")
ONE_WAY = frozenset({"one-shot", "monologue"})
CALL_STATUSES = ("recording", "processing", "delivered", "failed", "empty", "ended")


def usage_month(ts: float) -> str:
    """The calendar month (UTC) of a timestamp, as `YYYY-MM`: the quota period."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def purge_at(expires_at: float | None) -> datetime | None:
    """The `purge_at` value (UTC datetime, what the TTL indexes read) for an expiry timestamp; None: never."""
    return None if expires_at is None else datetime.fromtimestamp(expires_at, UTC)


class CloudModeStore:
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
        self.nodes: AsyncCollection = db["nodes"]
        self.agents: AsyncCollection = db["cloud_agents"]
        self.calls: AsyncCollection = db["cloud_calls"]
        self.entries: AsyncCollection = db["cloud_call_entries"]
        self.push_targets: AsyncCollection = db["cloud_push_targets"]
        self.usage: AsyncCollection = db["usage"]
        self.node_stats: AsyncCollection = db["node_stats"]

    async def ensure_indexes(self) -> None:
        await self.nodes.create_index([("url", ASCENDING)], unique=True, name="url")
        await self.nodes.create_index([("secret_hash", ASCENDING)], unique=True, name="secret_hash")
        await self.nodes.create_index(
            [("prev_secret_hash", ASCENDING)],
            partialFilterExpression={"prev_secret_hash": {"$exists": True}},
            name="prev_secret_hash",
        )
        await self.agents.create_index([("account", ASCENDING), ("slug", ASCENDING)], unique=True, name="account_slug")
        await self.agents.create_index([("account", ASCENDING), ("position", ASCENDING)], name="account_position")
        await self.calls.create_index(
            [("account", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)], name="account_created"
        )
        # At most one open call per account: the unique index decides a race, not a check before the insert.
        await self.calls.create_index(
            [("account", ASCENDING)], unique=True, partialFilterExpression={"open": True}, name="one_open_call"
        )
        await self.calls.create_index(
            [("node", ASCENDING), ("created_at", ASCENDING)], partialFilterExpression={"open": True}, name="node_open"
        )
        # The sweeper looks for the calls a node left in `processing`.
        await self.calls.create_index(
            [("node", ASCENDING)], partialFilterExpression={"status": "processing"}, name="node_processing"
        )
        await self.calls.create_index([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at")
        await self.entries.create_index([("account", ASCENDING), ("term_hashes", ASCENDING)], name="account_terms")
        await self.entries.create_index([("call", ASCENDING), ("seq", ASCENDING)], name="call_seq")
        await self.entries.create_index([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at")
        await self.push_targets.create_index([("account", ASCENDING)], name="account")
        await self.usage.create_index([("account", ASCENDING)], name="account")
        await self.node_stats.create_index([("node", ASCENDING), ("hour", ASCENDING)], name="node_hour")
        await self.node_stats.create_index([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at")

    async def delete_account(self, account: str) -> None:
        """Deletes everything cloud mode keeps for `account`: agents, calls, history entries, push targets and usage
        (nodes are not the account's). An account without any deletes nothing; repeating is harmless."""
        for collection in (self.agents, self.calls, self.entries, self.push_targets, self.usage):
            await collection.delete_many({"account": account})
