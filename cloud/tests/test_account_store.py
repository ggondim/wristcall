"""AccountStore: accounts, identities, devices, enroll/web collections cleanup and the v2 migration."""

import asyncio

import pytest

from wristcall_cloud.account_store import (
    SCHEMA_VERSION,
    AccountStore,
    DeviceLimit,
    KeyExists,
    KeyInUse,
    NoKey,
    RefreshReused,
    WrongKey,
)
from wristcall_cloud.session_tokens import secret_hash

NOW = 1_800_000_000.0
ISSUER = "https://auth.test"


def h(name: str) -> str:
    return secret_hash(name)


@pytest.fixture
async def store(mongo_db):
    s = AccountStore(mongo_db)
    await s.ensure_indexes()
    return s


def device(name="iPhone", platform="ios"):
    return {"name": name, "platform": platform}


async def test_indexes(store, mongo_db):
    accounts = await mongo_db["accounts"].index_information()
    assert accounts["key_hash"]["unique"] is True
    assert accounts["key_hash"]["partialFilterExpression"] == {"key_hash": {"$exists": True}}
    assert accounts["kind_created_at"]["key"] == [("kind", 1), ("created_at", 1)]
    assert accounts["last_seen_at"]["key"] == [("last_seen_at", 1)]
    identities = await mongo_db["identities"].index_information()
    assert identities["kind_issuer_subject"]["unique"] is True
    assert identities["kind_issuer_subject"]["key"] == [("kind", 1), ("issuer", 1), ("subject", 1)]
    assert identities["account"]["key"] == [("account", 1)]
    devices = await mongo_db["devices"].index_information()
    assert devices["account"]["key"] == [("account", 1)]
    assert devices["refresh_hash"]["unique"] is True
    assert devices["prev_refresh_hash"]["partialFilterExpression"] == {"prev_refresh_hash": {"$exists": True}}


async def test_create_account_shape_and_unique_key(store):
    doc = await store.create_account(h("k1"), now=NOW)
    assert doc["_id"].startswith("acc_") and len(doc["_id"]) == 4 + 26
    assert doc["created_at"] == NOW and doc["last_seen_at"] == NOW
    assert doc["key_hash"] == h("k1") and doc["key_set_at"] == NOW and doc["kind"] == "anonymous"
    assert await store.get_account(doc["_id"]) == doc
    with pytest.raises(KeyInUse):
        await store.create_account(h("k1"), now=NOW)


async def test_account_by_key(store):
    doc = await store.create_account(h("k1"), now=NOW)
    assert (await store.account_by_key(h("k1")))["_id"] == doc["_id"]
    assert await store.account_by_key(h("other")) is None
    assert await store.get_account("acc_missing") is None


async def test_touch_writes_at_most_hourly(store):
    doc = await store.create_account(h("k1"), now=NOW)
    await store.touch(doc["_id"], now=NOW + 10)
    assert (await store.get_account(doc["_id"]))["last_seen_at"] == NOW
    await store.touch(doc["_id"], now=NOW + 3601)
    assert (await store.get_account(doc["_id"]))["last_seen_at"] == NOW + 3601


async def test_touch_fills_a_missing_last_seen(store, mongo_db):
    await mongo_db["accounts"].insert_one({"_id": "acc_x", "created_at": NOW})
    await store.touch("acc_x", now=NOW + 5)
    assert (await store.get_account("acc_x"))["last_seen_at"] == NOW + 5


async def test_set_key_rules(store):
    a = await store.create_account(h("ka"), now=NOW)
    b = await store.create_account(h("kb"), now=NOW)
    legacy_id = "https://auth.test#s1"
    await store.resolve_zitadel(ISSUER, "s1", legacy_id=True, now=NOW)
    # no key yet: sets it
    await store.set_key(legacy_id, h("kl"), now=NOW + 1)
    doc = await store.get_account(legacy_id)
    assert doc["key_hash"] == h("kl") and doc["key_set_at"] == NOW + 1
    # same key: nothing changes
    await store.set_key(legacy_id, h("kl"), now=NOW + 99)
    assert (await store.get_account(legacy_id))["key_set_at"] == NOW + 1
    # another key on an account that has one
    with pytest.raises(KeyExists):
        await store.set_key(legacy_id, h("zzz"), now=NOW + 2)
    # a key that belongs to another account
    await store.accounts.update_one({"_id": legacy_id}, {"$unset": {"key_hash": "", "key_set_at": ""}})
    with pytest.raises(KeyInUse):
        await store.set_key(legacy_id, h("ka"), now=NOW + 3)
    assert "key_hash" not in await store.get_account(legacy_id)
    assert a["_id"] != b["_id"]


async def test_rotate_key_drops_other_devices_and_codes(store, mongo_db):
    acc = (await store.create_account(h("old"), now=NOW))["_id"]
    other = (await store.create_account(h("other"), now=NOW))["_id"]
    keep, _ = await store.add_device(acc, device("keep"), max_devices=10, now=NOW)
    await store.add_device(acc, device("watch", "watchos"), max_devices=10, now=NOW)
    await store.add_device(acc, device("web", "web"), max_devices=10, now=NOW)
    foreign, _ = await store.add_device(other, device(), max_devices=10, now=NOW)
    await mongo_db["enroll_codes"].insert_many([{"_id": "c1", "account": acc}, {"_id": "c2", "account": other}])
    await mongo_db["web_sessions"].insert_many(
        [
            {"_id": "w1", "account": acc, "status": "approved"},
            {"_id": "w2", "status": "pending"},
            {"_id": "w3", "account": other, "status": "approved"},
        ]
    )
    revoked = await store.rotate_key(acc, h("old"), h("new"), keep["_id"], now=NOW + 5)
    assert revoked == 2
    assert [d["_id"] for d in await store.list_devices(acc)] == [keep["_id"]]
    assert await store.device(other, foreign["_id"]) is not None
    doc = await store.get_account(acc)
    assert doc["key_hash"] == h("new") and doc["key_set_at"] == NOW + 5
    assert await store.account_by_key(h("old")) is None
    assert [d["_id"] async for d in mongo_db["enroll_codes"].find()] == ["c2"]
    assert sorted([d["_id"] async for d in mongo_db["web_sessions"].find()]) == ["w2", "w3"]


async def test_rotate_key_needs_the_current_hash(store):
    acc = (await store.create_account(h("old"), now=NOW))["_id"]
    dev, _ = await store.add_device(acc, device(), max_devices=10, now=NOW)
    other_dev, _ = await store.add_device(acc, device("b"), max_devices=10, now=NOW)
    with pytest.raises(WrongKey):
        await store.rotate_key(acc, h("nope"), h("new"), dev["_id"], now=NOW)
    assert (await store.get_account(acc))["key_hash"] == h("old")
    assert len(await store.list_devices(acc)) == 2
    await store.accounts.update_one({"_id": acc}, {"$unset": {"key_hash": ""}})
    with pytest.raises(NoKey):
        await store.rotate_key(acc, h("old"), h("new"), dev["_id"], now=NOW)
    assert other_dev["_id"]


async def test_rotate_key_to_a_key_of_another_account(store):
    acc = (await store.create_account(h("old"), now=NOW))["_id"]
    await store.create_account(h("taken"), now=NOW)
    dev, _ = await store.add_device(acc, device(), max_devices=10, now=NOW)
    await store.add_device(acc, device("b"), max_devices=10, now=NOW)
    with pytest.raises(KeyInUse):
        await store.rotate_key(acc, h("old"), h("taken"), dev["_id"], now=NOW)
    assert (await store.get_account(acc))["key_hash"] == h("old")
    assert len(await store.list_devices(acc)) == 2


async def test_count_created_since(store):
    await store.create_account(h("a"), now=NOW)
    await store.create_account(h("b"), now=NOW + 100)
    await store.resolve_zitadel(ISSUER, "z", legacy_id=False, now=NOW + 100)
    assert await store.count_created_since(NOW + 50) == 1
    assert await store.count_created_since(NOW) == 2


async def test_migrate_creates_zitadel_identities_once(store, mongo_db):
    await mongo_db["accounts"].insert_many(
        [
            {"_id": "https://auth.test#a", "created_at": 1.0, "last_seen_at": 2.0},
            {"_id": "https://auth.test#b", "created_at": 1.0, "last_seen_at": 2.0},
            {"_id": "acc_" + "a" * 26, "created_at": 1.0, "last_seen_at": 2.0, "kind": "anonymous"},
        ]
    )
    before = sorted([d async for d in mongo_db["accounts"].find()], key=lambda d: d["_id"])
    assert await store.migrate(now=NOW) == 2
    assert await store.migrate(now=NOW + 1) == 0
    assert SCHEMA_VERSION == 2
    meta = await mongo_db["meta"].find_one({"_id": "schema"})
    assert meta["version"] == 2 and meta["migrated_at"] == NOW + 1
    assert sorted([d async for d in mongo_db["accounts"].find()], key=lambda d: d["_id"]) == before
    assert await store.account_for_identity("zitadel", ISSUER, "a") == "https://auth.test#a"
    assert await store.account_for_identity("zitadel", ISSUER, "b") == "https://auth.test#b"
    idents = await store.identities_of("https://auth.test#a")
    assert len(idents) == 1 and idents[0]["_id"].startswith("idn_") and len(idents[0]["_id"]) == 4 + 16
    assert idents[0]["created_at"] == NOW


async def test_migrate_picks_up_accounts_created_after_a_rollback(store, mongo_db):
    await mongo_db["accounts"].insert_one({"_id": "https://auth.test#a", "created_at": 1.0, "last_seen_at": 2.0})
    assert await store.migrate(now=NOW) == 1
    await mongo_db["accounts"].insert_one({"_id": "https://auth.test#c", "created_at": 1.0, "last_seen_at": 2.0})
    assert await store.migrate(now=NOW + 1) == 1
    assert await store.account_for_identity("zitadel", ISSUER, "c") == "https://auth.test#c"


async def test_resolve_zitadel_adopts_an_unmigrated_legacy_account(store, mongo_db):
    await mongo_db["accounts"].insert_one({"_id": "https://auth.test#d", "created_at": 1.0, "last_seen_at": 2.0})
    assert await store.resolve_zitadel(ISSUER, "d", legacy_id=False, now=NOW) == "https://auth.test#d"
    assert await store.account_for_identity("zitadel", ISSUER, "d") == "https://auth.test#d"
    assert await mongo_db["accounts"].count_documents({}) == 1


async def test_resolve_zitadel_legacy_and_new(store, mongo_db):
    legacy = await store.resolve_zitadel(ISSUER, "s1", legacy_id=True, now=NOW)
    assert legacy == "https://auth.test#s1"
    assert await store.resolve_zitadel(ISSUER, "s1", legacy_id=True, now=NOW + 1) == legacy
    assert await store.resolve_zitadel(ISSUER, "s1", legacy_id=False, now=NOW + 2) == legacy
    assert (await store.get_account(legacy))["kind"] == "zitadel"
    new = await store.resolve_zitadel(ISSUER, "s2", legacy_id=False, now=NOW)
    assert new.startswith("acc_")
    assert (await store.get_account(new))["kind"] == "zitadel"
    assert await store.resolve_zitadel(ISSUER, "s2", legacy_id=False, now=NOW + 1) == new
    # a migrated account is found through its identity
    await mongo_db["accounts"].insert_one({"_id": "https://auth.test#m", "created_at": 1.0, "last_seen_at": 2.0})
    await store.migrate(now=NOW)
    assert await store.resolve_zitadel(ISSUER, "m", legacy_id=False, now=NOW) == "https://auth.test#m"


async def test_resolve_zitadel_race_ends_on_one_account(store, mongo_db):
    results = await asyncio.gather(*[store.resolve_zitadel(ISSUER, "race", legacy_id=False, now=NOW) for _ in range(6)])
    assert len(set(results)) == 1
    assert await mongo_db["identities"].count_documents({}) == 1
    assert await mongo_db["accounts"].count_documents({}) == 1
    results = await asyncio.gather(*[store.resolve_zitadel(ISSUER, "race2", legacy_id=True, now=NOW) for _ in range(6)])
    assert set(results) == {"https://auth.test#race2"}
    assert await mongo_db["identities"].count_documents({}) == 2
    assert await mongo_db["accounts"].count_documents({}) == 2


async def test_add_identity_never_moves(store):
    a = (await store.create_account(h("a"), now=NOW))["_id"]
    b = (await store.create_account(h("b"), now=NOW))["_id"]
    assert await store.add_identity(a, "apple", "https://appleid.apple.com", "sub1", now=NOW) == a
    assert await store.add_identity(b, "apple", "https://appleid.apple.com", "sub1", now=NOW) == a
    assert await store.account_for_identity("apple", "https://appleid.apple.com", "sub1") == a
    assert await store.identities_of(b) == []


async def test_add_device_limit(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    doc, refresh = await store.add_device(
        acc, {"name": "Watch", "platform": "watchos", "public_key": "pk"}, max_devices=2, now=NOW
    )
    assert doc["_id"].startswith("dev_") and doc["account"] == acc
    assert doc["name"] == "Watch" and doc["platform"] == "watchos" and doc["public_key"] == "pk"
    assert doc["created_at"] == NOW and doc["last_seen_at"] == NOW
    assert refresh.startswith("wcr_") and doc["refresh_hash"] == secret_hash(refresh)
    assert "prev_refresh_hash" not in doc
    await store.add_device(acc, device(), max_devices=2, now=NOW)
    with pytest.raises(DeviceLimit):
        await store.add_device(acc, device(), max_devices=2, now=NOW)
    other = (await store.create_account(h("b"), now=NOW))["_id"]
    await store.add_device(other, device(), max_devices=2, now=NOW)  # limit is per account


async def test_device_lookup_is_scoped_to_the_account(store):
    a = (await store.create_account(h("a"), now=NOW))["_id"]
    b = (await store.create_account(h("b"), now=NOW))["_id"]
    doc, _ = await store.add_device(a, device(), max_devices=10, now=NOW)
    assert (await store.device(a, doc["_id"]))["_id"] == doc["_id"]
    assert await store.device(b, doc["_id"]) is None
    assert await store.delete_device(b, doc["_id"]) is False
    assert await store.delete_device(a, doc["_id"]) is True
    assert await store.delete_device(a, doc["_id"]) is False


async def test_touch_device_writes_at_most_hourly(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    doc, _ = await store.add_device(acc, device(), max_devices=10, now=NOW)
    await store.touch_device(doc["_id"], now=NOW + 10)
    assert (await store.device(acc, doc["_id"]))["last_seen_at"] == NOW
    await store.touch_device(doc["_id"], now=NOW + 3601)
    assert (await store.device(acc, doc["_id"]))["last_seen_at"] == NOW + 3601


async def test_list_devices_by_created_at(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    second, _ = await store.add_device(acc, device("two"), max_devices=10, now=NOW + 2)
    first, _ = await store.add_device(acc, device("one"), max_devices=10, now=NOW + 1)
    assert [d["_id"] for d in await store.list_devices(acc)] == [first["_id"], second["_id"]]


async def test_rotate_refresh_rotates(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    doc, old = await store.add_device(acc, device(), max_devices=10, now=NOW)
    rotated = await store.rotate_refresh(old, idle_s=1000, now=NOW + 10)
    assert rotated is not None
    new_doc, new = rotated
    assert new.startswith("wcr_") and new != old
    assert new_doc["_id"] == doc["_id"] and new_doc["last_seen_at"] == NOW + 10
    assert new_doc["refresh_hash"] == secret_hash(new) and new_doc["prev_refresh_hash"] == secret_hash(old)
    again = await store.rotate_refresh(new, idle_s=1000, now=NOW + 20)
    assert again is not None and again[1] not in (old, new)


async def test_refresh_reuse_revokes_device(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    doc, old = await store.add_device(acc, device(), max_devices=10, now=NOW)
    survivor, _ = await store.add_device(acc, device("b"), max_devices=10, now=NOW)
    _, new = await store.rotate_refresh(old, idle_s=1000, now=NOW + 10)
    with pytest.raises(RefreshReused):
        await store.rotate_refresh(old, idle_s=1000, now=NOW + 20)
    assert await store.device(acc, doc["_id"]) is None
    assert await store.device(acc, survivor["_id"]) is not None
    # the revoked device's current token is gone too
    assert await store.rotate_refresh(new, idle_s=1000, now=NOW + 30) is None


async def test_refresh_of_unknown_token_is_none(store):
    assert await store.rotate_refresh("wcr_nothing", idle_s=1000, now=NOW) is None


async def test_refresh_race_one_wins_other_revokes(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    doc, token = await store.add_device(acc, device(), max_devices=10, now=NOW)
    results = await asyncio.gather(
        store.rotate_refresh(token, idle_s=1000, now=NOW + 1),
        store.rotate_refresh(token, idle_s=1000, now=NOW + 1),
        return_exceptions=True,
    )
    assert sum(isinstance(r, RefreshReused) for r in results) <= 1
    assert sum(isinstance(r, tuple) for r in results) == 1


async def test_refresh_of_idle_device_is_none(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    doc, token = await store.add_device(acc, device(), max_devices=10, now=NOW)
    assert await store.rotate_refresh(token, idle_s=100, now=NOW + 101) is None
    assert await store.device(acc, doc["_id"]) is not None  # idle is not revoked
    assert await store.rotate_refresh(token, idle_s=100, now=NOW + 100) is not None


async def test_delete_account_data_leaves_other_accounts(store, mongo_db):
    a = (await store.create_account(h("a"), now=NOW))["_id"]
    b = (await store.create_account(h("b"), now=NOW))["_id"]
    for acc in (a, b):
        await store.add_device(acc, device(), max_devices=10, now=NOW)
        await store.add_identity(acc, "apple", "https://appleid.apple.com", f"sub-{acc}", now=NOW)
        await mongo_db["enroll_codes"].insert_one({"_id": f"code-{acc}", "account": acc})
        await mongo_db["web_sessions"].insert_one({"_id": f"web-{acc}", "account": acc, "status": "approved"})
        await mongo_db["entitlements"].insert_one({"_id": acc, "plan": "full"})
    await store.delete_account_data(a)
    assert await store.get_account(a) is None
    assert await store.list_devices(a) == [] and await store.identities_of(a) == []
    for name in ("enroll_codes", "web_sessions"):
        assert await mongo_db[name].count_documents({"account": a}) == 0
        assert await mongo_db[name].count_documents({"account": b}) == 1
    assert await mongo_db["entitlements"].find_one({"_id": a}) is None
    assert await mongo_db["entitlements"].find_one({"_id": b}) is not None
    assert await store.get_account(b) is not None
    assert len(await store.list_devices(b)) == 1 and len(await store.identities_of(b)) == 1
