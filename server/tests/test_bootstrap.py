import asyncio
import threading

import pytest

from wristcall.bootstrap import IMPORT_MARK, bootstrap, profile_slug
from wristcall.config import parse_config
from wristcall.pairing import hash_secret
from wristcall.storage import open_sqlite_storage

PROVIDERS = {
    "speaches": {"type": "fake_stt"},
    "litellm": {"type": "echo_chat"},
    "xtts": {"type": "tone_tts"},
}

# Shape of the production config (3gr4m meta-infra stacks/wristcall/wristcall.yaml), fake providers.
PROD_PROFILES = {
    "default": {
        "display_name": "Agente",
        "language": "pt",
        "fallback_message": "Desculpe, não consegui responder agora.",
        "stt": "speaches",
        "responder": "litellm",
        "tts": "xtts",
        "system_prompt": "Responda em até três frases curtas.",
        "vad": {"silence_ms": 2000},
        "timeouts": {"stt_s": 30, "first_token_s": 20, "tts_s": 30},
    }
}


def config(profiles=None):
    data = {"server": {"public_url": "https://wc.test"}, "providers": PROVIDERS}
    if profiles is not None:
        data["profiles"] = profiles
    return parse_config(data, {})


@pytest.fixture
async def st():
    s = open_sqlite_storage(":memory:")
    yield s
    await s.close()


def legacy_device(st, device_id="d0", token="old-token"):
    st.db.execute(
        "INSERT INTO devices (id, name, token_hash, created_at) VALUES (?, 'Watch', ?, 1.0)", (device_id, hash_secret(token))
    )


async def test_production_upgrade_imports_default_and_adopts_the_watch(st):
    legacy_device(st)
    report = await bootstrap(st, config(PROD_PROFILES), now=lambda: 50.0)
    assert report.owner_created == "owner" and report.imported == ["default"] and report.adopted == 1
    (owner,) = await st.users.list()
    (agent,) = await st.agents.list(owner.id)
    assert (agent.slug, agent.display_name, agent.icon, agent.call_type, agent.position) == ("default", "Agente", "waveform", "conversation", 0)
    spec = agent.spec
    assert spec["stt"] == {"provider": "speaches"} and spec["action"] == {"provider": "litellm"} and spec["tts"] == {"provider": "xtts"}
    assert spec["language"] == "pt" and spec["vad"]["silence_ms"] == 2000 and spec["timeouts"]["stt_s"] == 30
    assert spec["fallback_message"] == "Desculpe, não consegui responder agora." and spec["turn_end"] == "auto"
    assert (await st.devices.by_token(hash_secret("old-token"))).user_id == owner.id
    assert await st.meta.get(IMPORT_MARK) == "50.0"


async def test_import_runs_once(st):
    await bootstrap(st, config(PROD_PROFILES))
    (owner,) = await st.users.list()
    await st.agents.delete(owner.id, (await st.agents.list(owner.id))[0].id)  # the owner deleted the imported agent
    report = await bootstrap(st, config(PROD_PROFILES))
    assert report.imported == [] and report.profiles_ignored is True
    assert await st.agents.list(owner.id) == []


async def test_default_comes_first_and_order_is_kept(st):
    profiles = {
        "coach": {"display_name": "Coach"},
        "default": {"display_name": "Agent", "stt": "speaches", "responder": "litellm", "tts": "xtts"},
        "demo": {"display_name": "Demo"},
    }
    await bootstrap(st, config(profiles))
    (owner,) = await st.users.list()
    assert [a.slug for a in await st.agents.list(owner.id)] == ["default", "coach", "demo"]


async def test_import_into_the_existing_user(st):
    await st.users.create("u_g", "gustavo", "Gustavo", 1.0)
    report = await bootstrap(st, config(PROD_PROFILES))
    assert report.owner_created is None
    assert [a.slug for a in await st.agents.list("u_g")] == ["default"]


async def test_no_profiles_no_user(st):
    legacy_device(st)
    report = await bootstrap(st, config())
    assert report.imported == [] and report.adopted == 0
    assert await st.users.list() == []
    assert await st.meta.get(IMPORT_MARK) is None


async def test_orphans_are_adopted_only_with_a_single_user(st):
    await st.users.create("u_a", "alice", "Alice", 1.0)
    await st.users.create("u_b", "bob", "Bob", 2.0)
    legacy_device(st)
    assert (await bootstrap(st, config())).adopted == 0
    assert (await st.devices.by_token(hash_secret("old-token"))).user_id is None


async def test_rollback_then_upgrade_again_adopts_new_orphans(st):
    await bootstrap(st, config(PROD_PROFILES))
    legacy_device(st, "d9", "paired-by-0.2.0")  # paired while the server ran 0.2.0 again
    report = await bootstrap(st, config(PROD_PROFILES))
    assert report.adopted == 1 and report.imported == []


def test_concurrent_bootstraps_import_once(tmp_path):
    # Server and CLI starting together: separate connections in separate threads.
    stores = [open_sqlite_storage(tmp_path) for _ in range(4)]
    errors: list[BaseException] = []

    def run(store) -> None:
        try:
            asyncio.run(bootstrap(store, config(PROD_PROFILES)))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    asyncio.run(_check_single_import(stores[0]))


async def _check_single_import(store) -> None:
    users = await store.users.list()
    assert [u.handle for u in users] == ["owner"]
    assert [a.slug for a in await store.agents.list(users[0].id)] == ["default"]


@pytest.mark.parametrize(
    "name, slug",
    [("default", "default"), ("My Coach", "my-coach"), ("Ünïcode!", "n-code"), ("___", "agent"), ("x" * 40, "x" * 32)],
)
def test_profile_slug(name, slug):
    assert profile_slug(name) == slug
