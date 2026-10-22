"""The managed Cloud entry and the default "Voice notes" agent an account gets with a plan (redeem, admin)."""

import asyncio
import functools
import time
import re
from dataclasses import replace

import pytest

from node_fixtures import db, node_config, served_node, signing_key  # noqa: F401
from test_entitlements_admin import anonymous, parsed
from test_sessions_api import bearer, new_key_b64
from wristcall_cloud import admin, sessions_api
from wristcall_cloud.account_store import AccountStore
from wristcall_cloud.cloud_store import CloudModeStore
from wristcall_cloud.config import CloudConfig, ProvisionSettings, provision_settings
from wristcall_cloud.provision import (
    CLOUD_ENTRY_NAME,
    VOICE_NOTES_SLUG,
    cloud_entry,
    cloud_entry_view,
    is_managed,
    provision,
    voice_notes_language,
)
from wristcall_cloud.store import Store

NOW = 1_800_000_000.0
SETTINGS = ProvisionSettings("https://cloud.test", "en", "stt-default")
VOICE_NOTES_SPEC = {"language": "en", "stt": {"provider": "stt-default"}, "action": None, "retention_days": None}


@pytest.fixture
async def store(mongo_db):
    s = AccountStore(mongo_db)
    await s.ensure_indexes()
    await Store(mongo_db).ensure_indexes()
    await CloudModeStore(mongo_db).ensure_indexes()
    return s


@pytest.fixture
def stores(mongo_db):
    return Store(mongo_db), CloudModeStore(mongo_db)


async def servers_of(mongo_db, account):
    return await mongo_db["servers"].find({"account": account}).to_list(None)


async def agents_of(mongo_db, account):
    return await mongo_db["cloud_agents"].find({"account": account}).sort("position", 1).to_list(None)


async def test_provision_full_creates_entry_and_voice_notes(store, stores, mongo_db):
    servers, cloud = stores
    assert await provision(servers, cloud, "acc_a", "full", SETTINGS, now=NOW) == {"entry": True, "voice_notes": True}
    [entry] = await servers_of(mongo_db, "acc_a")
    assert {k: entry[k] for k in ("name", "kind", "url", "linked", "managed", "agents")} == {
        "name": CLOUD_ENTRY_NAME, "kind": "cloud", "url": "https://cloud.test", "linked": True, "managed": True,
        "agents": [],
    }  # fmt: skip
    assert entry["name"] == "Wristcall Cloud" and entry["voice_notes_at"] == NOW
    assert re.fullmatch(r"srv_[0-9a-f]{12}", entry["_id"]) and is_managed(entry)
    assert (await cloud_entry(servers, "acc_a"))["_id"] == entry["_id"]
    [agent] = await agents_of(mongo_db, "acc_a")
    assert agent["slug"] == VOICE_NOTES_SLUG == "voice-notes"
    assert (agent["display_name"], agent["icon"], agent["color"], agent["call_type"]) == (
        "Voice notes", "note.text", "yellow", "one-shot",
    )  # fmt: skip
    assert agent["spec"] == VOICE_NOTES_SPEC
    assert re.fullmatch(r"ag_[0-9a-f]{12}", agent["_id"]) and agent["position"] == 0
    assert agent["created_at"] == agent["updated_at"] == NOW


async def test_provision_custom_creates_entry_only(store, stores, mongo_db):
    servers, cloud = stores
    assert await provision(servers, cloud, "acc_a", "custom", SETTINGS) == {"entry": True, "voice_notes": False}
    [entry] = await servers_of(mongo_db, "acc_a")
    assert "voice_notes_at" not in entry
    assert await agents_of(mongo_db, "acc_a") == []
    assert await provision(servers, cloud, "acc_a", "full", SETTINGS) == {"entry": False, "voice_notes": True}
    assert [a["slug"] for a in await agents_of(mongo_db, "acc_a")] == ["voice-notes"]
    assert len(await servers_of(mongo_db, "acc_a")) == 1


async def test_provision_is_idempotent_and_never_recreates_voice_notes(store, stores, mongo_db):
    servers, cloud = stores
    assert await provision(servers, cloud, "acc_a", "full", SETTINGS) == {"entry": True, "voice_notes": True}
    assert await provision(servers, cloud, "acc_a", "full", SETTINGS) == {"entry": False, "voice_notes": False}
    assert len(await servers_of(mongo_db, "acc_a")) == 1 and len(await agents_of(mongo_db, "acc_a")) == 1
    await mongo_db["cloud_agents"].delete_many({"account": "acc_a"})
    assert await provision(servers, cloud, "acc_a", "full", SETTINGS) == {"entry": False, "voice_notes": False}
    assert await agents_of(mongo_db, "acc_a") == []


async def test_concurrent_provisioning_creates_one_of_each(store, stores, mongo_db):
    servers, cloud = stores
    results = await asyncio.gather(*(provision(servers, cloud, "acc_a", "full", SETTINGS) for _ in range(5)))
    assert sum(r["entry"] for r in results) == 1 and sum(r["voice_notes"] for r in results) == 1
    assert len(await servers_of(mongo_db, "acc_a")) == 1 and len(await agents_of(mongo_db, "acc_a")) == 1


async def test_existing_slug_is_left_alone(store, stores, mongo_db):
    servers, cloud = stores
    mine = {"_id": "ag_mine", "account": "acc_a", "slug": "voice-notes", "display_name": "Mine", "position": 3}
    await mongo_db["cloud_agents"].insert_one(mine)
    assert await provision(servers, cloud, "acc_a", "full", SETTINGS, now=NOW) == {"entry": True, "voice_notes": False}
    assert await agents_of(mongo_db, "acc_a") == [mine]
    [entry] = await servers_of(mongo_db, "acc_a")
    assert entry["voice_notes_at"] == NOW


async def test_voice_notes_position_follows_the_last_agent(store, stores, mongo_db):
    servers, cloud = stores
    await mongo_db["cloud_agents"].insert_one({"_id": "ag_x", "account": "acc_a", "slug": "x", "position": 4})
    await provision(servers, cloud, "acc_a", "full", SETTINGS)
    assert [(a["slug"], a["position"]) for a in await agents_of(mongo_db, "acc_a")] == [("x", 4), ("voice-notes", 5)]


async def test_provision_language(store, stores, mongo_db):
    servers, cloud = stores
    await provision(servers, cloud, "acc_a", "full", SETTINGS, language="pt")
    [agent] = await agents_of(mongo_db, "acc_a")
    assert agent["spec"]["language"] == "pt"


def test_voice_notes_language():
    assert voice_notes_language("BR", "en") == "pt"
    assert voice_notes_language("US", "en") == "en"
    assert voice_notes_language(None, "es") == "es"
    assert all(voice_notes_language(c, "en") == "pt" for c in ("PT", "AO", "MZ", "CV", "GW", "ST", "TL"))


def test_is_managed():
    assert is_managed({"kind": "cloud", "managed": True})
    assert not is_managed({"kind": "cloud"})
    assert not is_managed({"kind": "cloud", "managed": "true"})
    assert not is_managed({"kind": "self-hosted", "managed": True})


async def test_cloud_entry_view_reads_cloud_agents(store, stores, mongo_db):
    servers, cloud = stores
    await provision(servers, cloud, "acc_a", "full", SETTINGS, now=NOW)
    await mongo_db["cloud_agents"].insert_one(
        {
            "_id": "ag_second", "account": "acc_a", "slug": "second", "display_name": "Second", "icon": "star",
            "color": None, "call_type": "conversation", "position": 1, "spec": {"secret": "x"},
            "created_at": NOW, "updated_at": NOW,
        }
    )  # fmt: skip
    await mongo_db["cloud_agents"].insert_one({"_id": "ag_other", "account": "acc_b", "slug": "o", "position": 0})
    entry = await cloud_entry(servers, "acc_a")
    view = await cloud_entry_view(cloud, entry)
    assert set(view) == {"id", "name", "url", "kind", "linked", "agents", "created_at", "updated_at"}
    assert (view["id"], view["name"], view["kind"], view["url"]) == (
        entry["_id"], "Wristcall Cloud", "cloud", "https://cloud.test",
    )  # fmt: skip
    voice, second = view["agents"]
    assert voice == {
        "id": voice["id"], "slug": "voice-notes", "display_name": "Voice notes", "icon": "note.text",
        "color": "yellow", "call_type": "one-shot",
    }  # fmt: skip
    assert second == {
        "id": "ag_second", "slug": "second", "display_name": "Second", "icon": "star", "call_type": "conversation",
    }  # fmt: skip
    assert "spec" not in str(view) and "account" not in view


async def test_cloud_entry_ignores_unmanaged_cloud_servers(store, stores, mongo_db):
    servers, _ = stores
    await mongo_db["servers"].insert_one(
        {"_id": "srv_old", "account": "acc_a", "kind": "cloud", "url": "https://old.test", "name": "Old"}
    )
    assert await cloud_entry(servers, "acc_a") is None


async def test_user_server_with_the_cloud_url_is_left_alone(store, stores, mongo_db):
    servers, cloud = stores
    mine = {
        "_id": "srv_mine", "account": "acc_a", "name": "Mine", "url": "https://cloud.test", "kind": "self-hosted",
        "linked": False, "agents": [], "created_at": NOW, "updated_at": NOW,
    }  # fmt: skip
    await mongo_db["servers"].insert_one(mine)
    assert await provision(servers, cloud, "acc_a", "full", SETTINGS) == {"entry": False, "voice_notes": False}
    assert await servers_of(mongo_db, "acc_a") == [mine]
    assert await agents_of(mongo_db, "acc_a") == []


def test_provision_settings_of_config():
    assert provision_settings(CloudConfig(mongo_url="mongodb://x")) is None
    assert provision_settings(CloudConfig(mongo_url="mongodb://x", public_url="https://cloud.test")) == SETTINGS


# Redeem


def invite(client, plan: str = "full") -> str:
    return client.portal.call(functools.partial(client.app.state.accounts.create_invites, plan, 1, days=30))[0]


def signed_up(client, **headers: str) -> tuple[str, dict[str, str]]:
    """A new silent account: (account id, headers with its access token and `headers`)."""
    body = {"account_key": new_key_b64(), "device": {"name": "phone", "platform": "ios"}}
    r = client.post("/v1/accounts", json=body)
    assert r.status_code == 201, r.text
    return r.json()["account"], {**bearer(r.json()["access_token"]), **headers}


def test_redeem_provisions_with_country_language(node_config, mongo_db, fake_verifier, signing_key):  # noqa: F811
    config = replace(node_config, client_country_header="CF-IPCountry")
    for c in served_node(config, mongo_db, fake_verifier, signing_key):
        account, headers = signed_up(c, **{"CF-IPCountry": "BR"})
        r = c.post("/v1/entitlements/redeem", json={"code": invite(c)}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"plan": "full", "source": "beta", "expires_at": None}
        with db(mongo_db) as d:
            [agent] = list(d["cloud_agents"].find({"account": account}))
            [entry] = list(d["servers"].find({"account": account}))
        assert agent["slug"] == "voice-notes" and agent["spec"]["language"] == "pt"
        assert is_managed(entry) and entry["url"] == "https://cloud.test"


def test_redeem_elsewhere_uses_the_default_language(node_config, mongo_db, fake_verifier, signing_key):  # noqa: F811
    config = replace(node_config, client_country_header="CF-IPCountry")
    for c in served_node(config, mongo_db, fake_verifier, signing_key):
        account, headers = signed_up(c, **{"CF-IPCountry": "US"})
        assert c.post("/v1/entitlements/redeem", json={"code": invite(c)}, headers=headers).status_code == 200
        with db(mongo_db) as d:
            [agent] = list(d["cloud_agents"].find({"account": account}))
        assert agent["spec"]["language"] == "en"


def test_redeem_still_answers_when_provisioning_fails(
    node_config, mongo_db, fake_verifier, signing_key, monkeypatch, caplog  # noqa: F811
):
    async def broken(*args, **kwargs):
        raise RuntimeError("https://cloud.test secret")

    monkeypatch.setattr(sessions_api, "provision", broken)
    for c in served_node(node_config, mongo_db, fake_verifier, signing_key):
        account, headers = signed_up(c)
        r = c.post("/v1/entitlements/redeem", json={"code": invite(c)}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"plan": "full", "source": "beta", "expires_at": None}
        with db(mongo_db) as d:
            assert d["entitlements"].find_one({"_id": account})["plan"] == "full"
    assert "provisioning after redeem failed (RuntimeError)" in caplog.text
    assert "cloud.test" not in caplog.text and "secret" not in caplog.text


# Admin


async def cli(store, capsys, *argv, settings=None):
    code = await admin.run(admin.build_parser().parse_args(list(argv)), store, settings=settings)
    out = capsys.readouterr()
    return code, out.out, out.err


async def test_admin_grant_provisions(store, mongo_db, capsys):
    account = await anonymous(store, "a", seen=NOW)
    code, out, err = await cli(store, capsys, "grant", account, "--plan", "full", settings=SETTINGS)
    assert code == 0 and err == ""
    assert parsed(out)["plan"] == "full" and parsed(out)["provisioned"] == "entry=true voice_notes=true"
    other = await anonymous(store, "b", seen=NOW)
    code, out, _ = await cli(store, capsys, "grant", other, "--plan", "full")
    assert code == 0 and parsed(out)["provisioned"] == "skipped"
    assert await servers_of(mongo_db, other) == []
    code, out, _ = await cli(store, capsys, "grant", other, "--plan", "full", "--language", "pt", settings=SETTINGS)
    assert parsed(out)["provisioned"] == "entry=true voice_notes=true"
    [agent] = await agents_of(mongo_db, other)
    assert agent["spec"]["language"] == "pt"


async def test_admin_grant_and_provision_all(store, mongo_db, capsys):
    done = await anonymous(store, "done", seen=NOW)
    code, out, _ = await cli(store, capsys, "grant", done, "--plan", "full", settings=SETTINGS)
    assert parsed(out)["provisioned"] == "entry=true voice_notes=true"
    pending = await anonymous(store, "pending", seen=NOW)
    code, out, _ = await cli(store, capsys, "grant", pending, "--plan", "full")
    assert parsed(out)["provisioned"] == "skipped"
    planless = await anonymous(store, "planless", seen=NOW)
    expired = await anonymous(store, "expired", seen=NOW)
    await store.grant(expired, "full", days=1, now=time.time() - 2 * 86400)

    code, out, err = await cli(store, capsys, "provision", "--all", settings=SETTINGS)
    assert code == 0 and err == ""
    assert parsed(out) == {"accounts": "2", "entries_created": "1", "voice_notes_created": "1"}
    assert len(await agents_of(mongo_db, pending)) == 1
    assert await servers_of(mongo_db, planless) == [] and await servers_of(mongo_db, expired) == []

    code, out, err = await cli(store, capsys, "provision", planless, settings=SETTINGS)
    assert code == 1 and out == "" and "no plan" in err
    code, out, err = await cli(store, capsys, "provision", "acc_nope", settings=SETTINGS)
    assert code == 1 and out == "" and "no such account" in err
    code, out, err = await cli(store, capsys, "provision", pending)
    assert code == 2 and out == "" and "WRISTCALL_CLOUD_PUBLIC_URL is required" in err
    code, out, err = await cli(store, capsys, "provision", settings=SETTINGS)
    assert code == 2 and out == "" and err
    code, out, err = await cli(store, capsys, "provision", pending, "--all", settings=SETTINGS)
    assert code == 2 and out == "" and err

    code, out, _ = await cli(store, capsys, "provision", pending, settings=SETTINGS)
    assert code == 0 and parsed(out)["provisioned"] == "entry=false voice_notes=false"

    code, out, _ = await cli(store, capsys, "show", done)
    keys = list(parsed(out))
    assert parsed(out)["cloud_entry"] == "true" and keys.index("cloud_entry") < keys.index("cloud_agents")
    assert keys[-3:] == ["cloud_agents", "cloud_calls", "usage_month_s"]
    code, out, _ = await cli(store, capsys, "show", planless)
    assert parsed(out)["cloud_entry"] == "false"


async def test_admin_provision_one_account_with_language(store, mongo_db, capsys):
    account = await anonymous(store, "a", seen=NOW)
    await store.grant(account, "custom", days=None)
    code, out, _ = await cli(store, capsys, "provision", account, "--language", "pt", settings=SETTINGS)
    assert code == 0 and parsed(out)["provisioned"] == "entry=true voice_notes=false"
    assert await agents_of(mongo_db, account) == []


def test_admin_main_provision_needs_the_public_url(capsys):
    env = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:1"}
    assert admin.main(["provision", "--all"], {**env, "WRISTCALL_CLOUD_PUBLIC_URL": "http://cloud.test"}) == 2
    assert "WRISTCALL_CLOUD_PUBLIC_URL" in capsys.readouterr().err
    assert admin.main(["provision", "--all", "--language", "Portuguese"], env) == 2
    assert capsys.readouterr().err
