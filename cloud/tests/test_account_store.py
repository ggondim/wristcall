"""AccountStore: accounts, identities, devices, enroll/web collections cleanup and the v2 migration."""

import asyncio
import re
from datetime import UTC, datetime

import pytest

from wristcall_cloud.account_store import (
    SCHEMA_VERSION,
    AccountStore,
    AlreadyEntitled,
    CodeLimit,
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


async def test_reset_key_replaces_and_revokes(store, mongo_db):
    acc = (await store.create_account(h("theirs"), now=NOW))["_id"]
    other = (await store.create_account(h("other"), now=NOW))["_id"]
    keep, _ = await store.add_device(acc, device("keep"), max_devices=10, now=NOW)
    await store.add_device(acc, device("pad", "ipados"), max_devices=10, now=NOW)
    foreign, _ = await store.add_device(other, device(), max_devices=10, now=NOW)
    await mongo_db["enroll_codes"].insert_many([{"_id": "c1", "account": acc}, {"_id": "c2", "account": other}])
    await mongo_db["web_sessions"].insert_many(
        [{"_id": "w1", "account": acc, "status": "approved"}, {"_id": "w2", "account": acc, "status": "pending"}]
    )
    with pytest.raises(KeyInUse):
        await store.reset_key(acc, h("other"), keep["_id"], now=NOW + 1)
    assert (await store.get_account(acc))["key_hash"] == h("theirs")
    assert len(await store.list_devices(acc)) == 2
    assert await store.reset_key(acc, h("mine"), keep["_id"], now=NOW + 5) == 1
    doc = await store.get_account(acc)
    assert doc["key_hash"] == h("mine") and doc["key_set_at"] == NOW + 5
    assert await store.account_by_key(h("theirs")) is None
    assert [d["_id"] for d in await store.list_devices(acc)] == [keep["_id"]]
    assert await store.device(other, foreign["_id"]) is not None
    assert [d["_id"] async for d in mongo_db["enroll_codes"].find()] == ["c2"]
    assert [d["_id"] async for d in mongo_db["web_sessions"].find()] == ["w2"]
    # No key yet: the reset sets one.
    keyless = await store.resolve_zitadel(ISSUER, "z", legacy_id=False, now=NOW)
    assert await store.reset_key(keyless, h("first"), "dev_none", now=NOW) == 0
    assert (await store.get_account(keyless))["key_hash"] == h("first")


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
    assert SCHEMA_VERSION == 3
    meta = await mongo_db["meta"].find_one({"_id": "schema"})
    assert meta["version"] == 3 and meta["migrated_at"] == NOW + 1
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


async def test_delete_account_data_deletes_devices_after_the_rest(store, mongo_db, monkeypatch):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    dev, _ = await store.add_device(acc, device(), max_devices=10, now=NOW)
    await store.add_identity(acc, "apple", "https://appleid.apple.com", "sub", now=NOW)
    await mongo_db["enroll_codes"].insert_one({"_id": "code", "account": acc})
    await mongo_db["web_sessions"].insert_one({"_id": "web", "account": acc, "status": "approved"})
    await mongo_db["entitlements"].insert_one({"_id": acc, "plan": "full"})

    async def fail(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(store.devices, "delete_many", fail)
    with pytest.raises(RuntimeError):
        await store.delete_account_data(acc)
    assert await store.identities_of(acc) == []
    for name in ("enroll_codes", "web_sessions"):
        assert await mongo_db[name].count_documents({"account": acc}) == 0
    assert await mongo_db["entitlements"].find_one({"_id": acc}) is None
    # The caller's device (and its token) survives, so DELETE /v1/account can be retried.
    assert await store.device(acc, dev["_id"]) is not None
    assert await store.get_account(acc) is not None
    monkeypatch.undo()
    await store.delete_account_data(acc)
    assert await store.get_account(acc) is None and await store.list_devices(acc) == []


# Enroll codes


async def test_enroll_code_single_use_and_expiry(store, mongo_db):
    acc = (await store.create_account(h("k"), now=NOW))["_id"]
    code, expires_at = await store.add_enroll_code(acc, max_open=5, now=NOW)
    assert re.fullmatch(r"wce_[a-z2-7]{26}", code) and expires_at == NOW + 600
    doc = await mongo_db["enroll_codes"].find_one()
    assert doc["_id"] == h(code) and code not in str(doc)
    assert (doc["account"], doc["platforms"], doc["created_at"]) == (acc, ["watchos"], NOW)
    assert (await store.enroll_code(code, now=NOW + 1))["account"] == acc
    # Reading does not spend it; the wrong platform does not either.
    assert await store.take_enroll_code(code, "ios", now=NOW + 1) is None
    assert await store.take_enroll_code(code, "watchos", now=NOW + 1) == acc
    assert await store.take_enroll_code(code, "watchos", now=NOW + 2) is None
    assert await store.enroll_code(code, now=NOW + 2) is None
    late, _ = await store.add_enroll_code(acc, max_open=5, now=NOW)
    assert await store.enroll_code(late, now=NOW + 600) is None
    assert await store.take_enroll_code(late, "watchos", now=NOW + 600) is None
    assert await store.enroll_code("wce_unknown", now=NOW) is None


async def test_enroll_code_limit(store):
    acc = (await store.create_account(h("k"), now=NOW))["_id"]
    other = (await store.create_account(h("o"), now=NOW))["_id"]
    for _ in range(2):
        await store.add_enroll_code(acc, max_open=2, now=NOW)
    with pytest.raises(CodeLimit):
        await store.add_enroll_code(acc, max_open=2, now=NOW + 1)
    await store.add_enroll_code(other, max_open=2, now=NOW + 1)
    # Expired codes are not open any more.
    await store.add_enroll_code(acc, max_open=2, now=NOW + 600)


# Web sessions


async def test_web_session_lifecycle(store, mongo_db):
    acc = (await store.create_account(h("k"), now=NOW))["_id"]
    web = {"name": "Chrome on macOS", "platform": "web"}
    created = await store.add_web_session(web, "Mozilla/5.0", "BR", now=NOW)
    assert re.fullmatch(r"ws_[0-9a-f]{16}", created["id"]) and created["expires_at"] == NOW + 300
    assert created["approve_key"].startswith("wck_") and created["poll_key"].startswith("wcp_")
    doc = await mongo_db["web_sessions"].find_one({"_id": created["id"]})
    assert doc["approve_hash"] == h(created["approve_key"]) and doc["poll_hash"] == h(created["poll_key"])
    assert created["approve_key"] not in str(doc) and created["poll_key"] not in str(doc)
    assert (doc["device"], doc["user_agent"], doc["country"], doc["status"]) == (web, "Mozilla/5.0", "BR", "pending")
    sid, approve, poll = created["id"], created["approve_key"], created["poll_key"]
    seen = await store.web_session(sid, approve, now=NOW + 1)
    assert seen["device"] == web and seen["created_at"] == NOW
    pending = await store.take_web_session(sid, poll, now=NOW + 1)
    assert pending["status"] == "pending"
    assert await store.web_session(sid, approve, now=NOW + 2) is not None  # polling a pending one keeps it
    assert await store.approve_web_session(sid, approve, acc, now=NOW + 3) is True
    assert await store.approve_web_session(sid, approve, acc, now=NOW + 4) is False
    assert await store.web_session(sid, approve, now=NOW + 4) is None  # no longer pending
    approved = await store.take_web_session(sid, poll, now=NOW + 5)
    assert (approved["status"], approved["account"]) == ("approved", acc)
    assert await store.take_web_session(sid, poll, now=NOW + 6) is None
    late = await store.add_web_session(web, "ua", None, now=NOW)
    assert await store.web_session(late["id"], late["approve_key"], now=NOW + 300) is None
    assert await store.approve_web_session(late["id"], late["approve_key"], acc, now=NOW + 300) is False
    assert await store.take_web_session(late["id"], late["poll_key"], now=NOW + 300) is None


async def test_web_session_wrong_keys(store):
    acc = (await store.create_account(h("k"), now=NOW))["_id"]
    created = await store.add_web_session({"name": "w", "platform": "web"}, "ua", None, now=NOW)
    sid, approve, poll = created["id"], created["approve_key"], created["poll_key"]
    assert await store.web_session(sid, poll, now=NOW) is None
    assert await store.web_session("ws_0000000000000000", approve, now=NOW) is None
    assert await store.approve_web_session(sid, poll, acc, now=NOW) is False
    assert await store.take_web_session(sid, approve, now=NOW) is None
    assert await store.approve_web_session(sid, approve, acc, now=NOW) is True
    assert await store.take_web_session(sid, approve, now=NOW) is None  # the approve key never takes it
    assert (await store.take_web_session(sid, poll, now=NOW))["account"] == acc


# Invites and entitlements


async def test_invite_single_use(store, mongo_db):
    a = (await store.create_account(h("a"), now=NOW))["_id"]
    b = (await store.create_account(h("b"), now=NOW))["_id"]
    codes = await store.create_invites("full", 3, days=30, now=NOW)
    assert len(set(codes)) == 3
    assert all(re.fullmatch(r"WC-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", c) for c in codes)
    docs = await mongo_db["invites"].find().to_list(length=None)
    assert all(re.fullmatch(r"inv_[0-9a-f]{16}", d["_id"]) and d["used_by"] is None for d in docs)
    assert not any(c in str(docs) for c in codes)
    assert docs[0]["expires_at"] == NOW + 30 * 86400
    granted = await store.redeem_invite(codes[0], a, now=NOW + 1)
    assert (granted["plan"], granted["source"], granted["expires_at"]) == ("full", "beta", None)
    assert (await store.entitlement(a, now=NOW + 2))["plan"] == "full"
    used = await mongo_db["invites"].find_one({"used_by": a})
    assert used["used_at"] == NOW + 1 and granted["invite"] == used["_id"]
    assert await store.redeem_invite(codes[0], b, now=NOW + 2) is None
    with pytest.raises(AlreadyEntitled):
        await store.redeem_invite(codes[1], a, now=NOW + 2)
    assert await mongo_db["invites"].count_documents({"used_by": None}) == 2  # not spent
    assert await store.redeem_invite(codes[1], b, now=NOW + 30 * 86400) is None  # expired
    assert await store.redeem_invite("WC-AAAA-AAAA-AAAA", b, now=NOW) is None
    with pytest.raises(ValueError):
        await store.create_invites("gold", 1, days=30, now=NOW)


async def test_invite_normalization(store, mongo_db):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    await mongo_db["invites"].insert_one(
        {
            "_id": "inv_0000000000000000",
            "code_hash": h("WCABCDEFGHIJKL"),
            "plan": "custom",
            "created_at": NOW,
            "expires_at": NOW + 86400,
            "used_by": None,
            "used_at": None,
        }
    )
    assert (await store.redeem_invite(" wc-abcd efgh-ijkl ", acc, now=NOW))["plan"] == "custom"
    code = (await store.create_invites("full", 1, days=1, now=NOW))[0]
    other = (await store.create_account(h("b"), now=NOW))["_id"]
    assert (await store.redeem_invite(code.lower().replace("-", " "), other, now=NOW))["plan"] == "full"


async def test_entitlement_expiry(store):
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    assert await store.entitlement(acc, now=NOW) is None
    granted = await store.grant(acc, "custom", days=10, now=NOW)
    assert (granted["plan"], granted["source"], granted["granted_at"]) == ("custom", "beta", NOW)
    assert granted["expires_at"] == NOW + 10 * 86400
    assert (await store.entitlement(acc, now=NOW + 10 * 86400 - 1))["plan"] == "custom"
    assert await store.entitlement(acc, now=NOW + 10 * 86400) is None
    # An expired entitlement does not block an invite.
    code = (await store.create_invites("full", 1, days=30, now=NOW))[0]
    assert (await store.redeem_invite(code, acc, now=NOW + 10 * 86400))["plan"] == "full"
    forever = await store.grant(acc, "full", days=None, source="admin", now=NOW)
    assert (forever["expires_at"], forever["source"]) == (None, "admin")
    assert await store.entitlement(acc, now=NOW + 10**9) is not None
    assert await store.revoke_entitlement(acc) is True
    assert await store.revoke_entitlement(acc) is False
    assert await store.entitlement(acc, now=NOW) is None
    with pytest.raises(ValueError):
        await store.grant(acc, "gold", days=None, now=NOW)


async def test_ttl_indexes(store, mongo_db):
    for name in ("enroll_codes", "web_sessions"):
        index = (await mongo_db[name].index_information())["purge_at"]
        assert index["key"] == [("purge_at", 1)] and index["expireAfterSeconds"] == 0
    assert (await mongo_db["enroll_codes"].index_information())["account"]["key"] == [("account", 1)]
    invites = (await mongo_db["invites"].index_information())["code_hash"]
    assert invites["key"] == [("code_hash", 1)] and invites["unique"] is True
    acc = (await store.create_account(h("a"), now=NOW))["_id"]
    _, expires_at = await store.add_enroll_code(acc, max_open=5, now=NOW)
    code_doc = await mongo_db["enroll_codes"].find_one()
    assert code_doc["purge_at"] == datetime.fromtimestamp(expires_at + 3600, UTC)
    session = await store.add_web_session({"name": "w", "platform": "web"}, "ua", None, now=NOW)
    session_doc = await mongo_db["web_sessions"].find_one()
    assert session_doc["purge_at"] == datetime.fromtimestamp(session["expires_at"] + 3600, UTC)
