import logging
from dataclasses import replace

import pytest

from memory_storage import MemoryStorage
from wristcall.agents import Agent, AgentSpec, ProviderRef
from wristcall.history import KEY_ID, History, check_key
from wristcall.history_codec import HistoryCodec, HistoryKeyError

KEY = bytes(range(32))


def agent(call_type="conversation") -> Agent:
    spec = AgentSpec(stt=ProviderRef(provider="stt"), action=ProviderRef(provider="llm"), tts=ProviderRef(provider="tts"))
    return Agent(
        id="ag_1", user_id="u_a", slug="helper", display_name="Helper", icon="waveform", call_type=call_type,
        position=0, spec=spec, created_at=1.0, updated_at=1.0,
    )


@pytest.fixture
async def st():
    s = MemoryStorage()
    await s.users.create("u_a", "alice", "Alice", 1.0)
    return s


async def test_a_call_starts_open_with_the_agent_as_it_was(st):
    history = History(st, HistoryCodec(), now=lambda: 50.0)
    call_log = await history.start(agent(), "d_1")
    r = await st.calls.get("u_a", call_log.record.id)
    assert (r.status, r.call_type, r.device_id, r.agent_slug, r.agent_name, r.created_at) == (
        "recording", "conversation", "d_1", "helper", "Helper", 50.0
    )
    assert r.id.startswith("c_")


async def test_entries_are_sealed_at_rest_and_opened_for_the_owner(st):
    history = History(st, HistoryCodec(KEY))
    call_log = await history.start(agent(), None)
    await call_log.add("user", "comprar leite")
    await call_log.add("agent", "Anotado.", "tts_failed")
    await call_log.add("user", None, "stt_failed")
    stored = await st.calls.entries("u_a", call_log.record.id)
    assert [e.sealed for e in stored] == [True, True, False]
    assert "leite" not in stored[0].text
    view = await history.detail(await call_log.save(status="ended", finished=True))
    assert view["status"] == "ended" and view["text"] == "comprar leite"
    assert [(e["role"], e["text"], e["error"]) for e in view["entries"]] == [
        ("user", "comprar leite", None), ("agent", "Anotado.", "tts_failed"), ("user", None, "stt_failed"),
    ]
    found = await st.calls.list("u_a", terms=HistoryCodec(KEY).query_terms("LEITE"))
    assert [c.id for c in found] == [call_log.record.id]
    unreadable = await History(st, HistoryCodec()).entries(call_log.record)
    assert [(e.text, e.error) for e in unreadable] == [(None, "unreadable"), (None, "unreadable"), (None, "stt_failed")]


async def test_recording_an_utterance_never_fails_the_call(st, caplog):
    history = History(st, HistoryCodec())
    call_log = await history.start(agent(), None)

    async def boom(*a, **kw):
        raise RuntimeError("disk full")

    st.calls.add_entry = boom
    with caplog.at_level(logging.ERROR):
        await call_log.add("user", "segredo do usuário")
    assert "could not record utterance 0" in caplog.text
    assert "segredo" not in caplog.text


async def test_a_call_deleted_meanwhile_is_left_alone(st):
    history = History(st, HistoryCodec())
    call_log = await history.start(agent("one-shot"), None)
    await st.calls.delete("u_a", call_log.record.id)
    await call_log.add("user", "oi")
    done = await call_log.save(status="delivered", finished=True)
    assert done.status == "delivered"
    assert await st.calls.get("u_a", call_log.record.id) is None


async def test_the_first_key_is_remembered_and_others_refused(st):
    await check_key(st, HistoryCodec())  # no key, nothing sealed: fine, nothing recorded
    assert await st.meta.get(KEY_ID) is None
    await check_key(st, HistoryCodec(KEY))
    assert await st.meta.get(KEY_ID) == HistoryCodec(KEY).key_id
    await check_key(st, HistoryCodec(KEY))  # same key again
    with pytest.raises(HistoryKeyError, match="not the key"):
        await check_key(st, HistoryCodec(bytes(32)))
    with pytest.raises(HistoryKeyError, match="set history.encryption_key"):
        await check_key(st, HistoryCodec())


def settings(**kw):
    from wristcall.config import HistoryConfig

    return HistoryConfig(**kw)


async def test_a_call_expires_by_its_agent_retention(st):
    a = agent()
    history = History(st, HistoryCodec(), settings(default_retention_days=90, max_retention_days=365), now=lambda: 1000.0)
    assert (await history.start(a, None)).record.expires_at == 1000.0 + 90 * 86_400
    week = replace(a, spec=a.spec.model_copy(update={"retention_days": 7}))
    assert (await history.start(week, None)).record.expires_at == 1000.0 + 7 * 86_400
    forever = replace(a, spec=a.spec.model_copy(update={"retention_days": "forever"}))
    assert (await history.start(forever, None)).record.expires_at == 1000.0 + 365 * 86_400
    assert (await History(st, HistoryCodec()).start(forever, None)).record.expires_at is None


async def test_startup_applies_the_operator_settings_to_past_calls(st):
    await st.agents.create(agent().to_record())
    old = History(st, HistoryCodec(), now=lambda: 1000.0)  # kept forever back then
    mine = (await old.start(agent(), None)).record
    orphan = (await old.start(replace(agent(), id="ag_deleted"), None)).record
    now = History(st, HistoryCodec(), settings(default_retention_days=30, max_retention_days=60), now=lambda: 2000.0)
    await now.apply_retention()
    assert (await st.calls.get("u_a", mine.id)).expires_at == 1000.0 + 30 * 86_400
    assert (await st.calls.get("u_a", orphan.id)).expires_at == 1000.0 + 60 * 86_400  # only the ceiling


async def test_purge_deletes_expired_closed_calls(st):
    history = History(st, HistoryCodec(), settings(default_retention_days=1), now=lambda: 0.0)
    done = await history.start(agent(), None)
    await done.add("user", "velho")
    await done.save(status="ended", finished=True)
    still_open = await history.start(agent(), None)
    later = History(st, HistoryCodec(), settings(default_retention_days=1), now=lambda: 86_400.0)
    assert await later.purge() == 1
    assert await st.calls.get("u_a", done.record.id) is None
    assert await st.calls.get("u_a", still_open.record.id) is not None
