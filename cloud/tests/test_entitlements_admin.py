"""Beta entitlements admin CLI, the idle anonymous account cleanup and the `admin` subcommand."""

import asyncio
import re
import sys
import time

import pytest
from pymongo.errors import PyMongoError

from node_fixtures import db
from test_cloud_store import COLLECTIONS, cloud_mode_counts, seed_cloud_mode
from wristcall_cloud import admin
from wristcall_cloud.account_store import AccountStore, purge_idle_accounts_loop
from wristcall_cloud.cloud_store import usage_month
from wristcall_cloud.config import ConfigError, admin_settings
from wristcall_cloud.session_tokens import secret_hash

NOW = 1_800_000_000.0
DAY = 86400
IDLE = NOW - 90 * DAY
LONG_AGO = NOW - 200 * DAY
ISSUER = "https://auth.test"


@pytest.fixture
async def store(mongo_db):
    s = AccountStore(mongo_db)
    await s.ensure_indexes()
    return s


async def anonymous(store, name, *, seen):
    doc = await store.create_account(secret_hash(name), now=seen)
    return doc["_id"]


def device(name="iPhone", platform="ios"):
    return {"name": name, "platform": platform}


async def test_purge_idle_accounts_spares_identities_plans_and_current_accounts(store, mongo_db):
    idle = await anonymous(store, "idle", seen=LONG_AGO)
    planned = await anonymous(store, "planned", seen=LONG_AGO)
    linked = await anonymous(store, "linked", seen=LONG_AGO)
    fresh = await anonymous(store, "fresh", seen=NOW - DAY)
    current = f"{ISSUER}#x"
    await mongo_db["accounts"].insert_one({"_id": current, "created_at": LONG_AGO, "last_seen_at": LONG_AGO})
    await store.grant(planned, "full", days=None, now=LONG_AGO)
    await store.add_identity(linked, "zitadel", ISSUER, "sub-linked", now=LONG_AGO)
    for account in (idle, planned, linked):
        await store.add_device(account, device(), max_devices=10, now=LONG_AGO)
    await mongo_db["servers"].insert_one({"_id": "srv_1", "account": idle, "url": "https://a.test"})
    await mongo_db["servers"].insert_one({"_id": "srv_2", "account": planned, "url": "https://b.test"})

    assert await store.purge_idle_accounts(IDLE, now=NOW) == 1

    assert await store.get_account(idle) is None
    assert await store.list_devices(idle) == []
    assert await mongo_db["servers"].find_one({"account": idle}) is None
    for kept in (planned, linked, fresh, current):
        assert await store.get_account(kept) is not None
    # their devices were idle too, so those go (they could not refresh anyway); the accounts stay
    assert await store.list_devices(planned) == [] and await store.list_devices(linked) == []
    assert await mongo_db["servers"].find_one({"account": planned}) is not None
    assert await store.identities_of(linked)


async def test_idle_purge_removes_cloud_mode_data(store, mongo_db):
    idle = await anonymous(store, "idle", seen=LONG_AGO)
    current = await anonymous(store, "current", seen=NOW)
    with db(mongo_db) as d:
        seed_cloud_mode(d, idle)
        seed_cloud_mode(d, current)
    assert await store.purge_idle_accounts(IDLE, now=NOW) == 1
    with db(mongo_db) as d:
        assert cloud_mode_counts(d, idle) == dict.fromkeys(COLLECTIONS, 0)
        assert cloud_mode_counts(d, current) == dict.fromkeys(COLLECTIONS, 1)


async def test_purge_idle_accounts_takes_an_expired_plan_account(store):
    account = await anonymous(store, "expired", seen=LONG_AGO)
    await store.grant(account, "full", days=1, now=LONG_AGO)
    assert await store.purge_idle_accounts(IDLE, now=NOW) == 1
    assert await store.get_account(account) is None
    assert await store.entitlements.find_one({"_id": account}) is None


async def test_purge_idle_accounts_never_takes_zitadel_accounts(store):
    result = await store.resolve_zitadel(ISSUER, "someone", legacy_id=False, now=LONG_AGO)
    assert await store.purge_idle_accounts(IDLE, now=NOW) == 0
    assert await store.get_account(result) is not None


async def test_purge_claims_the_account_before_deleting(store, mongo_db):
    """An account found idle that is used before its turn comes loses nothing."""
    touched = await anonymous(store, "touched", seen=LONG_AGO)
    linked = await anonymous(store, "linked", seen=LONG_AGO)
    granted = await anonymous(store, "granted", seen=LONG_AGO)
    for account in (touched, linked, granted):
        await mongo_db["servers"].insert_one({"_id": "srv_" + account, "account": account, "url": "https://a.test"})
    await store.touch(touched, now=NOW)  # active again after the scan found it
    await store.add_identity(linked, "zitadel", ISSUER, "late", now=NOW)
    await store.grant(granted, "full", days=None, now=NOW)

    for account in (touched, linked, granted):
        assert await store._purge_account(account, IDLE, now=NOW) is False
        assert await store.get_account(account) is not None
        assert await mongo_db["servers"].find_one({"account": account}) is not None
    assert await store.identities_of(linked) and await store.entitlement(granted, now=NOW)


async def test_purge_idle_accounts_skips_one_touched_during_the_scan(store, mongo_db, monkeypatch):
    victim = await anonymous(store, "victim", seen=LONG_AGO)
    idle = await anonymous(store, "idle", seen=LONG_AGO)
    original = store._purge_account

    async def purge(account, before, *, now=None):
        if account == victim:
            await store.touch(victim, now=NOW)  # used between the find and its turn
        return await original(account, before, now=now)

    monkeypatch.setattr(store, "_purge_account", purge)
    assert await store.purge_idle_accounts(IDLE, now=NOW) == 1
    assert await store.get_account(victim) is not None and await store.get_account(idle) is None


async def test_purge_idle_devices(store):
    account = await anonymous(store, "owner", seen=NOW)
    old, _ = await store.add_device(account, device("old"), max_devices=10, now=LONG_AGO)
    new, _ = await store.add_device(account, device("new"), max_devices=10, now=NOW)
    assert await store.purge_idle_accounts(IDLE, now=NOW) == 0
    assert [d["_id"] for d in await store.list_devices(account)] == [new["_id"]]
    assert old["_id"] != new["_id"]


async def test_purge_loop_survives_a_failure():
    calls: list[float] = []
    sleeps: list[float] = []

    class FakeStore:
        async def purge_idle_accounts(self, before: float) -> int:
            calls.append(before)
            if len(calls) == 1:
                raise PyMongoError("down")
            return 2

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await purge_idle_accounts_loop(FakeStore(), every_s=60, idle_s=1000, sleep=sleep, now=lambda: 5000.0)
    assert sleeps == [60] * 3
    assert calls == [4000.0, 4000.0]


async def cli(store, capsys, *argv):
    code = await admin.run(admin.build_parser().parse_args(list(argv)), store)
    out = capsys.readouterr()
    return code, out.out, out.err


def parsed(out: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in out.splitlines())


async def test_admin_grant_show_revoke(store, capsys):
    account = await anonymous(store, "a", seen=NOW)
    await store.add_device(account, device(), max_devices=10)
    await store.add_identity(account, "zitadel", ISSUER, "s1")

    code, out, err = await cli(store, capsys, "grant", account, "--plan", "full", "--days", "30")
    assert code == 0 and err == "" and parsed(out)["plan"] == "full"
    assert (await store.entitlement(account))["plan"] == "full"

    code, out, _ = await cli(store, capsys, "show", account)
    shown = parsed(out)
    assert code == 0
    assert shown["account"] == account and shown["kind"] == "anonymous"
    assert shown["devices"] == "1" and shown["identities"] == "zitadel" and shown["plan"] == "full"
    assert {"created_at", "last_seen_at"} <= shown.keys()
    assert (shown["cloud_agents"], shown["cloud_calls"], shown["usage_month_s"]) == ("0", "0", "0")
    assert list(parsed(out))[-3:] == ["cloud_agents", "cloud_calls", "usage_month_s"]

    code, out, _ = await cli(store, capsys, "revoke", account)
    assert code == 0 and parsed(out)["revoked"] == "true"
    assert await store.entitlement(account) is None
    code, out, _ = await cli(store, capsys, "show", account)
    assert parsed(out)["plan"] == "none"
    code, out, _ = await cli(store, capsys, "revoke", account)
    assert code == 0 and parsed(out)["revoked"] == "false"


async def test_admin_invites_create_prints_codes_that_redeem(store, capsys):
    code, out, err = await cli(store, capsys, "invites", "create", "--plan", "custom", "--count", "3")
    assert code == 0 and err == ""
    codes = [v for k, v in (line.split(": ", 1) for line in out.splitlines()) if k == "code"]
    assert len(codes) == 3 and all(re.fullmatch(r"WC-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", c) for c in codes)
    account = await anonymous(store, "r", seen=NOW)
    granted = await store.redeem_invite(codes[0], account)
    assert granted["plan"] == "custom"
    # The stored invites hold no code in clear.
    assert all(codes[0] not in str(d) for d in await store.invites.find().to_list(None))


async def test_admin_grant_unknown_account_fails(store, capsys):
    for argv in (("grant", "acc_nope", "--plan", "full"), ("show", "acc_nope"), ("revoke", "acc_nope")):
        code, out, err = await cli(store, capsys, *argv)
        assert code == 1 and out == "" and "no such account" in err
    assert await store.entitlements.count_documents({}) == 0


async def test_admin_stats(store, capsys):
    a = await anonymous(store, "a", seen=NOW)
    await anonymous(store, "b", seen=LONG_AGO)
    await store.add_device(a, device(), max_devices=10)
    await store.add_identity(a, "zitadel", ISSUER, "s")
    await store.grant(a, "full", days=None)
    code, out, _ = await cli(store, capsys, "stats")
    stats = parsed(out)
    assert code == 0
    assert list(stats) == [
        "accounts", "anonymous_24h", "devices", "identities", "entitlements",
        "entitlements_by_country", "schema", "nodes", "nodes_enabled", "cloud_calls_open", "usage_month_s",
    ]  # fmt: skip
    assert stats["accounts"] == "2" and stats["devices"] == "1"
    assert stats["identities"] == "1" and stats["entitlements"] == "1"
    assert stats["entitlements_by_country"] == "unknown=1"  # `grant` has no caller to ask
    assert (stats["nodes"], stats["nodes_enabled"], stats["cloud_calls_open"], stats["usage_month_s"]) == (
        "0", "0", "0", "0"
    )


async def test_admin_stats_entitlements_by_country(store, capsys):
    now = time.time()
    code, out, _ = await cli(store, capsys, "stats")
    assert code == 0 and parsed(out)["entitlements_by_country"] == "none"
    accounts = [await anonymous(store, name, seen=now) for name in "abcdef"]
    for account, country in zip(accounts[:3], ("BR", "US", "BR")):
        codes = await store.create_invites("full", 1, days=30)
        await store.redeem_invite(codes[0], account, country=country)
    await store.grant(accounts[3], "full", days=None)  # CLI: no country
    codes = await store.create_invites("full", 1, days=30)
    await store.redeem_invite(codes[0], accounts[4])  # redeemed without a known country
    await store.grant(accounts[5], "full", days=1, now=now - 2 * DAY)  # expired: not counted
    await store.entitlements.update_one({"_id": accounts[5]}, {"$set": {"country": "AR"}})
    code, out, _ = await cli(store, capsys, "stats")
    stats = parsed(out)
    assert code == 0 and stats["entitlements"] == "5"
    assert stats["entitlements_by_country"] == "BR=2,US=1,unknown=2"


async def test_admin_stats_and_show_count_cloud_mode(store, mongo_db, capsys):
    now = time.time()
    month = usage_month(now)
    a = await anonymous(store, "a", seen=now)
    b = await anonymous(store, "b", seen=now)
    await store.migrate()
    await mongo_db["nodes"].insert_many([{"_id": "home", "enabled": True}, {"_id": "away", "enabled": False}])
    await mongo_db["cloud_agents"].insert_many(
        [{"_id": "ag_1", "account": a}, {"_id": "ag_2", "account": a}, {"_id": "ag_3", "account": b}]
    )
    await mongo_db["cloud_calls"].insert_many(
        [{"_id": "c_1", "account": a, "open": True}, {"_id": "c_2", "account": a}, {"_id": "c_3", "account": b}]
    )
    await mongo_db["usage"].insert_many(
        [
            {"_id": f"{a}:{month}", "account": a, "month": month, "total_s": 100},
            {"_id": f"{b}:{month}", "account": b, "month": month, "total_s": 25},
            {"_id": f"{a}:2001-01", "account": a, "month": "2001-01", "total_s": 999},
        ]
    )
    code, out, _ = await cli(store, capsys, "stats")
    stats = parsed(out)
    assert code == 0 and stats["schema"] == "3" and (stats["nodes"], stats["nodes_enabled"]) == ("2", "1")
    assert stats["cloud_calls_open"] == "1" and stats["usage_month_s"] == "125"
    code, out, _ = await cli(store, capsys, "show", a)
    shown = parsed(out)
    assert code == 0 and (shown["cloud_agents"], shown["cloud_calls"], shown["usage_month_s"]) == ("2", "2", "100")


def test_admin_usage_errors_exit_2(capsys):
    bad = (
        [],
        ["grant", "acc_x"],
        ["grant", "acc_x", "--plan", "gold"],
        ["invites", "create", "--plan", "full", "--count", "0"],
    )
    for argv in bad:
        assert admin.main(argv, {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:1"}) == 2
    assert capsys.readouterr().err


def test_admin_main_config_error_exits_2(capsys):
    assert admin.main(["stats"], {}) == 2
    assert "WRISTCALL_CLOUD_MONGO_URL is required" in capsys.readouterr().err


def test_admin_settings_needs_only_mongo():
    assert admin_settings({"WRISTCALL_CLOUD_MONGO_URL": "mongodb://h:1/"}) == ("mongodb://h:1/", "wristcall_cloud")
    env = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb+srv://h", "WRISTCALL_CLOUD_DATABASE": "other"}
    assert admin_settings(env) == ("mongodb+srv://h", "other")
    with pytest.raises(ConfigError, match="WRISTCALL_CLOUD_MONGO_URL is required"):
        admin_settings({})
    with pytest.raises(ConfigError, match="mongodb://"):
        admin_settings({"WRISTCALL_CLOUD_MONGO_URL": "http://h"})


def test_admin_main_runs_against_mongo(mongo_db, capsys):
    from conftest import mongo_url

    env = {"WRISTCALL_CLOUD_MONGO_URL": mongo_url(), "WRISTCALL_CLOUD_DATABASE": mongo_db.name}
    assert admin.main(["stats"], env) == 0
    assert parsed(capsys.readouterr().out)["accounts"] == "0"


def test_main_dispatches_admin(monkeypatch):
    from wristcall_cloud import __main__ as entry

    seen = {}

    def fake_main(argv, env):
        seen["argv"] = argv
        seen["env"] = env
        return 7

    monkeypatch.setattr(admin, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["wristcall-cloud", "admin", "stats", "--x"])
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code == 7
    assert seen["argv"] == ["stats", "--x"]


def test_lifespan_runs_the_account_purge_loop(app, mongo_db, monkeypatch):
    import wristcall_cloud.app as app_module
    from conftest import serve

    started = []

    async def loop(accounts, *, every_s, idle_s):
        started.append((type(accounts).__name__, every_s, idle_s))
        await asyncio.sleep(3600)

    monkeypatch.setattr(app_module, "purge_idle_accounts_loop", loop)
    for c in serve(app, mongo_db):
        assert c.get("/v1/health").status_code == 200
    assert started == [("AccountStore", 21600, 90 * 86400)]
