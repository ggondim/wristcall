import pytest
from pymongo.errors import DuplicateKeyError

from conftest import mongo_url
from node_fixtures import db, node_app, node_client, node_config, signing_key
from test_sessions_api import bearer, create_account
from wristcall_cloud.account_store import AccountStore
from wristcall_cloud.cloud_store import CALL_STATUSES, CALL_TYPES, ONE_WAY, CloudModeStore, purge_at, usage_month
from wristcall_cloud.config import CloudConfig, ConfigError, config_from_env

BASE_ENV = {"WRISTCALL_CLOUD_MONGO_URL": mongo_url(), "WRISTCALL_CLOUD_CLIENT_IOS": "ios"}
NODE_ENV = {
    "WRISTCALL_CLOUD_QUOTA_MINUTES": "300",
    "WRISTCALL_CLOUD_MAX_CONVERSATION_S": "1200",
    "WRISTCALL_CLOUD_MAX_ONE_WAY_S": "600",
    "WRISTCALL_CLOUD_NODE_REQUESTS_PER_MINUTE": "50",
    "WRISTCALL_CLOUD_MAX_CLOUD_AGENTS": "7",
    "WRISTCALL_CLOUD_HISTORY_RETENTION_DAYS": "30",
    "WRISTCALL_CLOUD_MAX_HISTORY_RETENTION_DAYS": "400",
}


def test_call_vocabulary():
    assert CALL_TYPES == ("conversation", "one-shot", "monologue")
    assert ONE_WAY == frozenset({"one-shot", "monologue"})
    assert CALL_STATUSES == ("recording", "processing", "delivered", "failed", "empty", "ended")


async def test_indexes_exist(mongo_db):
    cloud = CloudModeStore(mongo_db)
    await cloud.ensure_indexes()
    await cloud.ensure_indexes()  # idempotent
    expected = {
        cloud.nodes: {"url", "secret_hash", "prev_secret_hash"},
        cloud.agents: {"account_slug", "account_position"},
        cloud.calls: {"account_created", "one_open_call", "node_open", "node_processing", "purge_at"},
        cloud.entries: {"account_terms", "call_seq", "purge_at"},
        cloud.push_targets: {"account"},
        cloud.usage: {"account"},
    }
    for collection, names in expected.items():
        info = await collection.index_information()
        assert names <= set(info), collection.name
    calls = await cloud.calls.index_information()
    assert calls["one_open_call"]["unique"] is True
    assert calls["one_open_call"]["partialFilterExpression"] == {"open": True}
    assert calls["purge_at"]["expireAfterSeconds"] == 0
    assert (await cloud.entries.index_information())["purge_at"]["expireAfterSeconds"] == 0
    assert (await cloud.nodes.index_information())["url"]["unique"] is True
    assert (await cloud.nodes.index_information())["secret_hash"]["unique"] is True
    assert (await cloud.agents.index_information())["account_slug"]["unique"] is True


async def test_one_open_call_per_account(mongo_db):
    cloud = CloudModeStore(mongo_db)
    await cloud.ensure_indexes()
    await cloud.calls.insert_one({"account": "acc_x", "open": True})
    with pytest.raises(DuplicateKeyError):
        await cloud.calls.insert_one({"account": "acc_x", "open": True})
    await cloud.calls.insert_one({"account": "acc_x"})
    await cloud.calls.insert_one({"account": "acc_x"})


async def test_migrate_writes_schema_3(mongo_db):
    await AccountStore(mongo_db).migrate()
    assert (await mongo_db["meta"].find_one({"_id": "schema"}))["version"] == 3


def test_usage_month_is_utc():
    assert usage_month(1767225599.0) == "2025-12"
    assert usage_month(1767225600.0) == "2026-01"


def test_purge_at():
    assert purge_at(None) is None
    moment = purge_at(1767225600.0)
    assert moment.isoformat() == "2026-01-01T00:00:00+00:00"


def test_retention_days():
    cfg = CloudConfig(mongo_url="mongodb://x", history_retention_days=90, max_history_retention_days=365)
    assert cfg.retention_days(None) == 90
    assert cfg.retention_days("forever") == 365
    assert cfg.retention_days(30) == 30
    assert cfg.retention_days(1000) == 365


def test_config_defaults_for_node_settings():
    cfg = config_from_env(BASE_ENV)
    assert (cfg.quota_minutes, cfg.max_conversation_s, cfg.max_one_way_s) == (600, 3600, 1800)
    assert (cfg.node_requests_per_minute, cfg.max_cloud_agents) == (1200, 20)
    assert (cfg.history_retention_days, cfg.max_history_retention_days) == (90, 365)
    assert (cfg.node_stale_s, cfg.orphan_sweep_every_s, cfg.orphan_grace_s) == (120, 30, 30)


def test_config_from_env_reads_node_settings():
    cfg = config_from_env({**BASE_ENV, **NODE_ENV})
    assert (cfg.quota_minutes, cfg.max_conversation_s, cfg.max_one_way_s) == (300, 1200, 600)
    assert (cfg.node_requests_per_minute, cfg.max_cloud_agents) == (50, 7)
    assert (cfg.history_retention_days, cfg.max_history_retention_days) == (30, 400)
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_QUOTA_MINUTES"):
        config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_QUOTA_MINUTES": "0"})
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_HISTORY_RETENTION_DAYS"):
        config_from_env(
            {
                **BASE_ENV,
                "WRISTCALL_CLOUD_HISTORY_RETENTION_DAYS": "400",
                "WRISTCALL_CLOUD_MAX_HISTORY_RETENTION_DAYS": "365",
            }
        )


def test_node_body_limit(client):
    body = b"x" * (100 * 1024)
    headers = {"Content-Type": "application/json"}
    # A node route without a credential: 401 is the answer, as long as it is not 413.
    assert client.post("/v1/node/heartbeat", headers=headers, content=body).status_code == 401
    r = client.post("/v1/servers", headers=headers, content=body)
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"
    r = client.post("/v1/node/heartbeat", headers=headers, content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"


def test_app_exposes_cloud_store(app):
    assert isinstance(app.state.cloud, CloudModeStore)


COLLECTIONS = ("cloud_agents", "cloud_calls", "cloud_call_entries", "cloud_push_targets", "usage")


def seed_cloud_mode(d, account: str) -> None:
    """One document of the account in each cloud-mode collection (`d`: a synchronous database handle)."""
    d["cloud_agents"].insert_one({"_id": f"ag_{account}", "account": account, "slug": "a"})
    d["cloud_calls"].insert_one({"_id": f"c_{account}", "account": account, "status": "delivered"})
    d["cloud_call_entries"].insert_one({"_id": f"c_{account}:0", "account": account, "call": f"c_{account}"})
    d["cloud_push_targets"].insert_one({"_id": f"t_{account}", "account": account})
    d["usage"].insert_one({"_id": f"{account}:2026-10", "account": account, "month": "2026-10", "total_s": 60})


def cloud_mode_counts(d, account: str) -> dict[str, int]:
    return {name: d[name].count_documents({"account": account}) for name in COLLECTIONS}


async def test_delete_account_removes_cloud_mode_data(mongo_db):
    cloud = CloudModeStore(mongo_db)
    with db(mongo_db) as d:
        seed_cloud_mode(d, "acc_one")
        seed_cloud_mode(d, "acc_two")
    await cloud.delete_account("acc_one")
    await cloud.delete_account("acc_one")  # nothing left: no harm
    with db(mongo_db) as d:
        assert cloud_mode_counts(d, "acc_one") == dict.fromkeys(COLLECTIONS, 0)
        assert cloud_mode_counts(d, "acc_two") == dict.fromkeys(COLLECTIONS, 1)


def test_delete_account_route_removes_cloud_mode_data(node_client, mongo_db):
    mine, other = create_account(node_client), create_account(node_client)
    with db(mongo_db) as d:
        seed_cloud_mode(d, mine["account"])
        seed_cloud_mode(d, other["account"])
    assert node_client.delete("/v1/account", headers=bearer(mine["access_token"])).status_code == 204
    with db(mongo_db) as d:
        assert d["accounts"].count_documents({"_id": mine["account"]}) == 0
        assert cloud_mode_counts(d, mine["account"]) == dict.fromkeys(COLLECTIONS, 0)
        assert cloud_mode_counts(d, other["account"]) == dict.fromkeys(COLLECTIONS, 1)


async def test_account_data_deletion_starts_with_cloud_mode_data(mongo_db, monkeypatch):
    """A failure in the later steps leaves the account (and its devices) for a retry that finds nothing left."""
    accounts = AccountStore(mongo_db)
    await accounts.ensure_indexes()
    account = (await accounts.create_account("a" * 64, now=1.0))["_id"]
    with db(mongo_db) as d:
        seed_cloud_mode(d, account)

    async def fail(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(accounts.devices, "delete_many", fail)
    with pytest.raises(RuntimeError):
        await accounts.delete_account_data(account)
    with db(mongo_db) as d:
        assert cloud_mode_counts(d, account) == dict.fromkeys(COLLECTIONS, 0)
    assert await accounts.get_account(account) is not None
