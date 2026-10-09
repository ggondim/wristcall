"""MongoDB storage. `accounts` holds one document per central account; `servers` the servers linked to it."""

from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from . import __version__
from .config import CloudConfig


class Store:
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
        self.accounts: AsyncCollection = db["accounts"]
        self.servers: AsyncCollection = db["servers"]

    async def ensure_indexes(self) -> None:
        await self.servers.create_index([("account", ASCENDING), ("url", ASCENDING)], unique=True, name="account_url")
        await self.servers.create_index([("account", ASCENDING), ("created_at", ASCENDING)], name="account_created_at")


def open_store(config: CloudConfig) -> tuple[AsyncMongoClient, Store]:
    client: AsyncMongoClient = AsyncMongoClient(
        config.mongo_url, appname=f"wristcall-cloud/{__version__}", tz_aware=True, serverSelectionTimeoutMS=10000
    )
    return client, Store(client[config.database])
