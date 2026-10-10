"""MongoDB storage. `accounts` holds one document per central account; `servers` the servers linked to it;
`push_registrations` the push relay's registrations, by the hash of their push key."""

import secrets
import time
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from . import __version__
from .config import CloudConfig

MAX_REGISTRATIONS_PER_CHANNEL = 20


def public_server(doc: dict[str, Any]) -> dict[str, Any]:
    """A server as the API shows it: no account."""
    fields = ("name", "url", "kind", "linked", "agents", "created_at", "updated_at")
    return {"id": doc["_id"]} | {k: doc[k] for k in fields}


class Store:
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
        self.accounts: AsyncCollection = db["accounts"]
        self.servers: AsyncCollection = db["servers"]
        self.push_registrations: AsyncCollection = db["push_registrations"]

    async def ensure_indexes(self) -> None:
        await self.servers.create_index([("account", ASCENDING), ("url", ASCENDING)], unique=True, name="account_url")
        await self.servers.create_index([("account", ASCENDING), ("created_at", ASCENDING)], name="account_created_at")
        await self.push_registrations.create_index(
            [("channel", ASCENDING), ("created_at", ASCENDING)], name="channel_created_at"
        )
        await self.push_registrations.create_index([("last_sent_at", ASCENDING)], name="last_sent_at")

    # Every server query below filters by `account`: another account's id answers like a missing one.

    async def touch_account(self, key: str) -> dict[str, Any]:
        """Creates the account on first use and records that it was seen."""
        now = time.time()
        doc = await self.accounts.find_one_and_update(
            {"_id": key},
            {"$set": {"last_seen_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return doc

    async def count_servers(self, account: str) -> int:
        return await self.servers.count_documents({"account": account})

    async def delete_account(self, key: str) -> None:
        await self.servers.delete_many({"account": key})
        await self.accounts.delete_one({"_id": key})

    async def list_servers(self, account: str) -> list[dict[str, Any]]:
        cursor = self.servers.find({"account": account}).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return await cursor.to_list(length=None)

    async def add_server(self, account: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Raises pymongo's DuplicateKeyError when the account already has the URL."""
        now = time.time()
        doc = {
            "_id": f"srv_{secrets.token_hex(6)}",
            "account": account,
            **fields,
            "agents": [],
            "created_at": now,
            "updated_at": now,
        }
        await self.servers.insert_one(doc)
        return doc

    async def get_server(self, account: str, server_id: str) -> dict[str, Any] | None:
        return await self.servers.find_one({"_id": server_id, "account": account})

    async def update_server(self, account: str, server_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        return await self.servers.find_one_and_update(
            {"_id": server_id, "account": account},
            {"$set": {**changes, "updated_at": time.time()}},
            return_document=ReturnDocument.AFTER,
        )

    async def delete_server(self, account: str, server_id: str) -> bool:
        result = await self.servers.delete_one({"_id": server_id, "account": account})
        return result.deleted_count == 1

    # Push registrations: `_id` is the SHA-256 of the push key, `channel` the APNs token or the Web Push endpoint.

    async def add_registration(
        self, registration_id: str, fields: dict[str, Any], *, now: float | None = None,
        max_per_channel: int = MAX_REGISTRATIONS_PER_CHANNEL,
    ) -> dict[str, Any]:
        """Inserts the registration; past `max_per_channel` for its channel, the oldest ones go."""
        now = time.time() if now is None else now
        doc = {"_id": registration_id, **fields, "created_at": now, "last_sent_at": now}
        await self.push_registrations.insert_one(doc)
        # The new registration always stays, whatever its time says: the oldest of the others go.
        cursor = (
            self.push_registrations.find({"channel": doc["channel"], "_id": {"$ne": registration_id}}, {"_id": 1})
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .skip(max_per_channel - 1)
        )
        extra = [d["_id"] async for d in cursor]
        if extra:
            await self.push_registrations.delete_many({"_id": {"$in": extra}})
        return doc

    async def get_registration(self, registration_id: str) -> dict[str, Any] | None:
        return await self.push_registrations.find_one({"_id": registration_id})

    async def touch_registration(self, registration_id: str, *, now: float | None = None) -> None:
        await self.push_registrations.update_one(
            {"_id": registration_id}, {"$set": {"last_sent_at": time.time() if now is None else now}}
        )

    async def delete_registration(self, registration_id: str) -> bool:
        result = await self.push_registrations.delete_one({"_id": registration_id})
        return result.deleted_count == 1

    async def purge_idle_registrations(self, before: float) -> int:
        """Deletes the registrations without a send since `before` (a Unix time); returns how many."""
        result = await self.push_registrations.delete_many({"last_sent_at": {"$lt": before}})
        return result.deleted_count


def open_store(config: CloudConfig) -> tuple[AsyncMongoClient, Store]:
    client: AsyncMongoClient = AsyncMongoClient(
        config.mongo_url, appname=f"wristcall-cloud/{__version__}", tz_aware=True, serverSelectionTimeoutMS=10000
    )
    return client, Store(client[config.database])

