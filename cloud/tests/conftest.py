import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import AsyncMongoClient, MongoClient

from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.oidc import Identity, OidcError, OidcUnavailable
from wristcall_cloud.store import Store

ISSUER = "https://auth.test"
CLIENTS = {"ios": "client-ios", "pwa": "client-pwa", "watch": "client-watch"}


def mongo_url() -> str:
    return os.environ.get("WRISTCALL_CLOUD_TEST_MONGO_URL", "mongodb://localhost:27017")


class FakeVerifier:
    """Accepts the tokens in `tokens`; anything else is an OidcError, and everything is OidcUnavailable when down."""

    def __init__(self) -> None:
        self.tokens: dict[str, Identity] = {}
        self.down = False

    def add(self, token: str, subject: str, client_id: str | None = "client-ios") -> Identity:
        identity = Identity(issuer=ISSUER, subject=subject, client_id=client_id, expires_at=4102444800.0)
        self.tokens[token] = identity
        return identity

    async def verify(self, token: str) -> Identity:
        if self.down:
            raise OidcUnavailable("issuer unavailable")
        try:
            return self.tokens[token]
        except KeyError:
            raise OidcError("invalid token") from None


@pytest.fixture
def mongo_db():
    """A throwaway database, dropped at the end (with a sync client: the async one belongs to the app's loop)."""
    name = f"test_{uuid.uuid4().hex}"
    client = AsyncMongoClient(mongo_url(), serverSelectionTimeoutMS=5000, tz_aware=True)
    yield client[name]
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        sync.drop_database(name)


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), project_id="1234")


@pytest.fixture
def fake_verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture
def app(config, mongo_db, fake_verifier):
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier)


def serve(app, db):
    """TestClient with the lifespan; the async Mongo client binds to the TestClient's loop, so it closes there."""
    with TestClient(app) as c:
        yield c
        c.portal.call(db.client.close)


@pytest.fixture
def client(app, mongo_db):
    yield from serve(app, mongo_db)
