import httpx
import pytest

from wristcall.agents import (
    REDACTED,
    Agent,
    AgentError,
    AgentService,
    AgentSpec,
    CustomEndpoint,
    ProviderRef,
    agent_detail,
    agent_summary,
    build_agent_providers,
    spec_from_profile,
)
from wristcall.config import parse_config
from wristcall.providers import ProviderError
from wristcall.providers.fake import EchoChat, FakeStt, ToneTts
from wristcall.storage import open_sqlite_storage


def config(**over):
    data = {
        "server": {"public_url": "https://wc.test"},
        "providers": {
            "stt": {"type": "fake_stt", "text": "hi"},
            "llm": {"type": "echo_chat"},
            "tts": {"type": "tone_tts", "sample_rate": 16000},
        },
        **over,
    }
    return parse_config(data, {})


@pytest.fixture
async def svc():
    st = open_sqlite_storage(":memory:")
    await st.users.create("u_a", "alice", "Alice", 1.0)
    await st.users.create("u_b", "bob", "Bob", 1.0)
    async with httpx.AsyncClient() as http:
        yield AgentService(st, config(), http, now=lambda: 10.0)
    await st.close()


async def make(cfg, **kw):
    st = open_sqlite_storage(":memory:")
    await st.users.create("u_a", "alice", "Alice", 1.0)
    return AgentService(st, cfg, httpx.AsyncClient(), **kw)


async def test_create_with_defaults_uses_the_only_provider_of_each_kind(svc):
    a = await svc.create("u_a", {"slug": "helper"})
    assert (a.slug, a.display_name, a.icon, a.call_type, a.position) == ("helper", "helper", "waveform", "conversation", 0)
    assert a.id.startswith("ag_") and len(a.id) == 15
    assert a.spec.stt == ProviderRef(provider="stt")
    assert a.spec.action == ProviderRef(provider="llm")
    assert a.spec.tts == ProviderRef(provider="tts")
    assert a.spec.turn_end == "auto" and a.spec.vad.silence_ms == 800
    assert (a.created_at, a.updated_at) == (10.0, 10.0)


async def test_create_full_and_list_in_order(svc):
    await svc.create("u_a", {"slug": "first"})
    a = await svc.create(
        "u_a",
        {
            "slug": "coach", "display_name": "Coach", "icon": "figure.run", "language": "pt",
            "turn_end": "manual", "vad": {"silence_ms": 2000}, "timeouts": {"stt_s": 30},
            "system_prompt": "Be a coach.", "fallback_message": "Desculpe.",
        },
    )
    assert a.spec.vad.silence_ms == 2000 and a.spec.vad.min_speech_ms == 300
    assert a.spec.timeouts.stt_s == 30 and a.spec.timeouts.tts_s == 15
    assert [x.slug for x in await svc.list("u_a")] == ["first", "coach"]
    assert await svc.list("u_b") == []
    assert (await svc.default("u_a")).slug == "first"
    assert await svc.default("u_b") is None


async def test_create_with_position_moves_it(svc):
    await svc.create("u_a", {"slug": "first"})
    a = await svc.create("u_a", {"slug": "top", "position": 0})
    assert a.position == 0
    assert [(x.slug, x.position) for x in await svc.list("u_a")] == [("top", 0), ("first", 1)]


async def test_position_moves_and_renumbers(svc):
    for slug in ("a", "b", "c", "d"):
        await svc.create("u_a", {"slug": slug})
    moved = await svc.update("u_a", "d", {"position": 1})
    assert moved.position == 1
    assert [(x.slug, x.position) for x in await svc.list("u_a")] == [("a", 0), ("d", 1), ("b", 2), ("c", 3)]
    last = await svc.update("u_a", "a", {"position": 99, "display_name": "A"})
    assert (last.position, last.display_name) == (3, "A")
    assert [x.slug for x in await svc.list("u_a")] == ["d", "b", "c", "a"]


async def test_get_by_slug_or_id_and_ownership(svc):
    a = await svc.create("u_a", {"slug": "helper"})
    assert (await svc.get("u_a", "helper")).id == a.id
    assert (await svc.get("u_a", a.id)).slug == "helper"
    with pytest.raises(AgentError) as e:
        await svc.get("u_b", "helper")
    assert e.value.code == "not_found"


@pytest.mark.parametrize(
    "data, code, fragment",
    [
        ({}, "invalid", "slug is required"),
        ({"slug": "Bad Slug"}, "invalid", "slug"),
        ({"slug": "-x"}, "invalid", "slug"),
        ({"slug": "x" * 33}, "invalid", "slug"),
        ({"slug": "ok", "icon": "Not An Icon"}, "invalid", "icon"),
        ({"slug": "ok", "call_type": "one-shot"}, "unsupported", "one-shot"),
        ({"slug": "ok", "call_type": "podcast"}, "invalid", "call_type"),
        ({"slug": "ok", "turn_end": "sometimes"}, "invalid", "turn_end"),
        ({"slug": "ok", "colour": "red"}, "invalid", "colour"),
        ({"slug": "ok", "stt": {"provider": "nope"}}, "invalid", "nope"),
        ({"slug": "ok", "stt": {"provider": "llm"}}, "invalid", "responder, not stt"),
        ({"slug": "ok", "tts": {"type": "no_such_type"}}, "invalid", "no_such_type"),
        ({"slug": "ok", "stt": {"type": "openai_stt"}}, "invalid", "base_url"),
        ({"slug": "ok", "stt": {"provider": "stt", "type": "fake_stt"}}, "invalid", "not both"),
        ({"slug": "ok", "vad": {"silence_ms": "long"}}, "invalid", "silence_ms"),
        ({"slug": "ok", "display_name": ""}, "invalid", "display_name"),
    ],
)
async def test_create_rejects_invalid_input(svc, data, code, fragment):
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", data)
    assert e.value.code == code
    assert fragment in e.value.message


async def test_duplicate_slug_is_a_conflict_per_user(svc):
    await svc.create("u_a", {"slug": "helper"})
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "helper"})
    assert e.value.code == "conflict"
    assert (await svc.create("u_b", {"slug": "helper"})).user_id == "u_b"


async def test_agent_limit():
    svc = await make(config(limits={"max_agents_per_user": 1}))
    await svc.create("u_a", {"slug": "one"})
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "two"})
    assert e.value.code == "limit"


async def test_default_endpoint_needs_a_choice_when_ambiguous():
    cfg = config()
    data = cfg.model_dump()
    data["providers"]["stt2"] = {"type": "fake_stt"}
    svc = await make(parse_config(data, {}))
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "x"})
    assert "stt: choose a provider (this server offers: stt, stt2)" in e.value.message
    assert (await svc.create("u_a", {"slug": "x", "stt": {"provider": "stt2"}})).spec.stt.provider == "stt2"


async def test_custom_endpoint_is_stored_and_redacted(svc):
    stt = {"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w", "api_key": "sk-secret"}
    a = await svc.create("u_a", {"slug": "own", "stt": stt})
    assert isinstance(a.spec.stt, CustomEndpoint) and a.spec.stt.options()["api_key"] == "sk-secret"
    detail = agent_detail(a)
    assert detail["stt"] == {**stt, "api_key": REDACTED}
    assert "sk-secret" not in repr(detail)


async def test_custom_endpoints_can_be_turned_off():
    svc = await make(config(limits={"custom_endpoints": False}))
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "own", "stt": {"type": "fake_stt"}})
    assert "custom endpoints are off" in e.value.message


async def test_update_merges_and_keeps_redacted_secrets(svc):
    stt = {"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w", "api_key": "sk-secret"}
    a = await svc.create("u_a", {"slug": "own", "stt": stt, "vad": {"silence_ms": 900, "min_speech_ms": 400}})
    shown = agent_detail(a)
    shown_stt = {**shown["stt"], "model": "w2"}  # edited, still with "***"
    b = await svc.update("u_a", "own", {"slug": "mine", "stt": shown_stt, "vad": {"silence_ms": 1500}, "display_name": "Mine"})
    assert (b.id, b.slug, b.display_name) == (a.id, "mine", "Mine")
    assert b.spec.stt.options() == {"base_url": "https://stt.example/v1", "model": "w2", "api_key": "sk-secret"}
    assert (b.spec.vad.silence_ms, b.spec.vad.min_speech_ms) == (1500, 400)
    assert b.spec.action == a.spec.action
    assert b.created_at == a.created_at


async def test_update_to_another_type_does_not_reuse_the_secret(svc):
    stt = {"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w", "api_key": "sk-secret"}
    await svc.create("u_a", {"slug": "own", "stt": stt})
    b = await svc.update("u_a", "own", {"stt": {"type": "fake_stt", "api_key": REDACTED}})
    assert b.spec.stt.options() == {"api_key": REDACTED}


async def test_update_errors(svc):
    await svc.create("u_a", {"slug": "one"})
    await svc.create("u_a", {"slug": "two"})
    with pytest.raises(AgentError) as e:
        await svc.update("u_a", "two", {"slug": "one"})
    assert e.value.code == "conflict"
    with pytest.raises(AgentError) as e:
        await svc.update("u_a", "nope", {"display_name": "x"})
    assert e.value.code == "not_found"
    with pytest.raises(AgentError) as e:
        await svc.update("u_a", "one", {"call_type": "monologue"})
    assert e.value.code == "unsupported"


async def test_delete(svc):
    a = await svc.create("u_a", {"slug": "one"})
    with pytest.raises(AgentError):
        await svc.delete("u_b", a.id)
    await svc.delete("u_a", a.id)
    assert await svc.list("u_a") == []
    with pytest.raises(AgentError) as e:
        await svc.delete("u_a", "one")
    assert e.value.code == "not_found"


async def test_summary_has_no_endpoints_or_prompts(svc):
    a = await svc.create("u_a", {"slug": "one", "system_prompt": "secret plan"})
    assert agent_summary(a) == {
        "id": a.id, "slug": "one", "display_name": "one", "icon": "waveform", "call_type": "conversation", "turn_end": "auto",
    }


async def test_build_agent_providers():
    cfg = config()
    spec = AgentSpec(stt=ProviderRef(provider="stt"), action=CustomEndpoint(type="echo_chat"), tts=ProviderRef(provider="tts"))
    async with httpx.AsyncClient() as http:
        ps = build_agent_providers(cfg, spec, http)
        assert isinstance(ps.stt, FakeStt) and isinstance(ps.responder, EchoChat) and isinstance(ps.tts, ToneTts)
        broken = spec.model_copy(update={"tts": ProviderRef(provider="removed")})
        with pytest.raises(ProviderError, match="removed"):
            build_agent_providers(cfg, broken, http)


def test_spec_from_profile():
    cfg = config(profiles={"default": {"display_name": "A", "stt": "stt", "responder": "llm", "tts": "tts", "language": "pt", "vad": {"silence_ms": 2000}}})
    spec = spec_from_profile(cfg.profiles["default"])
    assert spec.action == ProviderRef(provider="llm") and spec.language == "pt" and spec.vad.silence_ms == 2000


async def test_record_round_trip(svc):
    a = await svc.create("u_a", {"slug": "own", "stt": {"type": "fake_stt", "text": "x"}})
    assert Agent.from_record(a.to_record()) == a


@pytest.mark.parametrize(
    "field, endpoint, expected, leak",
    [
        ("stt", {"type": "fake_stt", "warmup": "sk-LEAK1"}, "warmup is an operator setting", "sk-LEAK1"),
        (
            "stt",
            {"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w", "extra_form": "x"},
            "stt: invalid options for type 'openai_stt'",
            None,
        ),
        ("tts", {"type": "tone_tts", "sample_rate": "sk-LEAK2"}, "tts: invalid options for type 'tone_tts'", "sk-LEAK2"),
        ("stt", {"type": "openai_stt", "model": "w", "api_key": "sk-LEAK3"}, "stt: missing option 'base_url' for type 'openai_stt'", "sk-LEAK3"),
    ],
)
async def test_custom_endpoint_errors_are_invalid_and_never_echo_values(svc, field, endpoint, expected, leak):
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "own", field: endpoint})
    assert e.value.code == "invalid"
    assert expected in e.value.message
    if leak:
        assert leak not in e.value.message and leak not in str(e.value)
    assert e.value.__cause__ is None


async def test_build_agent_providers_wraps_custom_endpoint_failures():
    cfg = config()
    spec = AgentSpec(
        stt=CustomEndpoint(type="openai_stt", base_url="https://stt.example/v1", model="w", extra_form="sk-LEAK4"),
        action=ProviderRef(provider="llm"),
        tts=ProviderRef(provider="tts"),
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderError) as e:
            build_agent_providers(cfg, spec, http)
    assert str(e.value) == "invalid options for type 'openai_stt'"
    assert e.value.__cause__ is None
