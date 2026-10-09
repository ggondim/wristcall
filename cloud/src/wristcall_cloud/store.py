"""MongoDB storage. `accounts` holds one document per central account; `servers` the servers linked to it."""

import secrets
import time
from typing import Any

from pymongo import ASCENDING, AsyncMongoClient, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from . import __version__
from .config import CloudConfig


def public_server(doc: dict[str, Any]) -> dict[str, Any]:
    """A server as the API shows it: no account."""
    fields = ("name", "url", "kind", "linked", "agents", "created_at", "updated_at")
    return {"id": doc["_id"]} | {k: doc[k] for k in fields}


class Store:
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
        self.accounts: AsyncCollection = db["accounts"]
        self.servers: AsyncCollection = db["servers"]

    async def ensure_indexes(self) -> None:
        await self.servers.create_index([("account", ASCENDING), ("url", ASCENDING)], unique=True, name="account_url")
        await self.servers.create_index([("account", ASCENDING), ("created_at", ASCENDING)], name="account_created_at")

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


def open_store(config: CloudConfig) -> tuple[AsyncMongoClient, Store]:
    client: AsyncMongoClient = AsyncMongoClient(
        config.mongo_url, appname=f"wristcall-cloud/{__version__}", tz_aware=True, serverSelectionTimeoutMS=10000
    )
    return client, Store(client[config.database])

