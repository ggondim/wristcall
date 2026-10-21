"""Fixtures and helpers of the node API tests (`/v1/node/*`): a Cloud that signs server tokens as PUBLIC_URL, two
registered nodes (`home` at NODE_A, `away` at NODE_B), accounts with devices, and the headers a node sends.

A test module imports what it uses and the fixtures those depend on, e.g.
`from node_fixtures import node_app, node_client, node_config, nodes, signing_key`. Tests never sleep: age a
document by rewriting its fields with `db(mongo_db)` or hand `now` to the functions that take it.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pymongo import MongoClient
from pymongo.database import Database

from conftest import CLIENTS, ISSUER, mongo_url, serve
from test_sessions_api import create_account, enroll_by_key, new_key_b64, new_signing_key
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.nodes import NodeStore
from wristcall_cloud.push.channels import FakeChannel
from wristcall_cloud.signing import SigningKey, server_token_claims
from wristcall_cloud.store import Store

PUBLIC_URL = "https://cloud.test"
NODE_A = "https://node-a.test"
NODE_B = "https://node-b.test"


@pytest.fixture
def signing_key() -> SigningKey:
    return new_signing_key()


@pytest.fixture
def node_config() -> CloudConfig:
    # The fake push channel is given to the app below: `push_fake=True` needs a loopback public URL, and these
    # tests sign as an https one.
    return CloudConfig(
        mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), public_url=PUBLIC_URL, project_id="1234",
        legacy_zitadel_tokens=True,
    )


def build_node_app(config: CloudConfig, mongo_db, fake_verifier, signing_key: SigningKey):
    fake = FakeChannel()
    return create_app(
        config, store=Store(mongo_db), verifier=fake_verifier, signing_key=signing_key,
        channels={"apns": fake, "webpush": fake},
    )


@pytest.fixture
def node_app(node_config, mongo_db, fake_verifier, signing_key):
    return build_node_app(node_config, mongo_db, fake_verifier, signing_key)


@pytest.fixture
def node_client(node_app, mongo_db):
    yield from serve(node_app, mongo_db)


def served_node(config: CloudConfig, mongo_db, fake_verifier, signing_key: SigningKey) -> Iterator[Any]:
    """A TestClient for a node app built from `config` (use as `for c in served_node(...)`)."""
    yield from serve(build_node_app(config, mongo_db, fake_verifier, signing_key), mongo_db)


def register_nodes(client) -> dict[str, str]:
    """Registers `home` (NODE_A) and `away` (NODE_B) in the client's Cloud; returns their secrets."""
    store = NodeStore(client.app.state.cloud)

    async def add() -> dict[str, str]:
        secrets: dict[str, str] = {}
        for node_id, url in (("home", NODE_A), ("away", NODE_B)):
            _, secrets[node_id] = await store.add(
                node_id, url=url, region="gru", kind="always_on", capabilities=["operator_stt"]
            )
        return secrets

    return client.portal.call(add)


@pytest.fixture
def nodes(node_client) -> dict[str, str]:
    """The secrets of the registered nodes: `{"home": secret, "away": secret}`."""
    return register_nodes(node_client)


def new_account(client, platform: str = "ios") -> tuple[str, str, str]:
    """A silent account (the route of `create_account`): (account id, device id, account key)."""
    key = new_key_b64()
    created = create_account(client, key, platform)
    return created["account"], created["device"], key


def add_device(client, key: str, platform: str) -> str:
    """Enrolls another device on the account of `key` (a `watchos` one, say); returns its id."""
    r = enroll_by_key(client, key, platform)
    assert r.status_code == 201, r.text
    return r.json()["device"]


def user_token(
    key: SigningKey, account: str, device: str, platform: str, aud: str, *, iat: float | None = None, ttl_s: int = 300
) -> str:
    """The per-server token a device hands its node: `key` is the Cloud's signing key, `aud` the node's URL."""
    claims = server_token_claims(
        issuer=PUBLIC_URL, account=account, client_id=None, audience=aud,
        now=time.time() if iat is None else iat, ttl_s=ttl_s, device=device, platform=platform,
    )
    return key.sign(claims)


def node_headers(secret: str, *, user: str | None = None, grant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if user is not None:
        headers["X-Wristcall-User-Token"] = user
    if grant is not None:
        headers["X-Wristcall-Call-Grant"] = grant
    return headers


def grant_plan(mongo_db, account: str, plan: str = "full") -> None:
    """Gives the account an entitlement without end."""
    with db(mongo_db) as d:
        d["entitlements"].replace_one(
            {"_id": account},
            {"_id": account, "plan": plan, "source": "beta", "granted_at": time.time(), "expires_at": None},
            upsert=True,
        )


@contextmanager
def db(mongo_db) -> Iterator[Database]:
    """A synchronous handle on the same database (the async one belongs to the app's loop): to age documents and
    to read what was written. Use as `with db(mongo_db) as d:`."""
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000, tz_aware=True) as sync:
        yield sync[mongo_db.name]
