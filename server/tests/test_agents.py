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
        # The fake types too, so that tests can build custom endpoints without a network.
        "limits": {"custom_endpoint_types": [
            "openai_stt", "openai_chat", "openai_tts", "webhook", "fake_stt", "echo_chat", "tone_tts",
        ]},
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
        ({"slug": "ok", "call_type": "one-shot"}, "invalid", "action: choose a provider"),
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


async def test_placeholder_without_a_stored_secret_is_refused(svc):
    stt = {"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w", "api_key": "sk-secret"}
    await svc.create("u_a", {"slug": "own", "stt": stt})
    with pytest.raises(AgentError, match="only keeps a secret"):
        await svc.update("u_a", "own", {"stt": {"type": "fake_stt", "api_key": REDACTED}})  # another type: nothing to keep
    with pytest.raises(AgentError, match="only keeps a secret"):
        await svc.create("u_a", {"slug": "new", "stt": {**stt, "api_key": REDACTED}})
    with pytest.raises(AgentError, match="only keeps a secret"):
        await svc.update("u_a", "own", {"stt": {**stt, "api_key": REDACTED, "model": REDACTED}})  # not a secret key
    agent = await svc.get("u_a", "own")
    assert agent.spec.stt.options()["api_key"] == "sk-secret"


async def test_nested_secrets_are_redacted_and_kept():
    cfg = config()
    svc = await make(cfg)
    action = {
        "type": "openai_chat", "base_url": "https://llm.example/v1", "model": "m",
        "extra_body": {"metadata": {"session_token": "tok-nested"}, "max_tokens": 50},
    }
    a = await svc.create("u_a", {"slug": "own", "action": action})
    shown = agent_detail(a)["action"]
    assert shown["extra_body"] == {"metadata": {"session_token": REDACTED}, "max_tokens": 50}
    assert "tok-nested" not in repr(agent_detail(a))
    edited = {**shown, "extra_body": {**shown["extra_body"], "max_tokens": 80}}
    b = await svc.update("u_a", "own", {"action": edited})
    assert b.spec.action.options()["extra_body"] == {"metadata": {"session_token": "tok-nested"}, "max_tokens": 80}


@pytest.mark.parametrize(
    "key, secret",
    [("api_key", True), ("apiKey", True), ("x-api-key", True), ("access_token", True), ("client_secret", True),
     ("password", True), ("Authorization", True), ("apitoken", True), ("accesstoken", True), ("authtoken", True),
     ("secretkey", True), ("privatekey", True), ("apisecret", True), ("api_keys", True), ("secrets", True),
     ("passwords", True), ("auth", True), ("cookie", True), ("Set-Cookie", True), ("pwd", True), ("passphrase", True),
     ("client_credentials", True), ("keyword", True),  # over-redaction is fine: `***` round trips keep the value
     ("max_tokens", False), ("max_completion_tokens", False), ("max_output_tokens", False), ("min_tokens", False),
     ("maxTokens", False), ("tokenizer", False), ("tokenization", False), ("model", False), ("voice", False),
     ("base_url", False), ("sample_rate", False)],
)
def test_is_secret(key, secret):
    from wristcall.agents import _is_secret

    assert _is_secret(key) is secret


@pytest.mark.parametrize(
    "base_url",
    ["https://user:pass@stt.example/v1", "https://stt.example/v1?key=abc", "https://stt.example/v1#x", "ftp://stt.example", "stt.example"],
)
async def test_base_url_cannot_hold_secrets(svc, base_url):
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "own", "stt": {"type": "openai_stt", "base_url": base_url, "model": "w"}})
    assert e.value.code == "invalid" and "base_url" in e.value.message
    assert "pass" not in e.value.message and "abc" not in e.value.message


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
    assert e.value.code == "invalid" and "not webhook" in e.value.message


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


@pytest.mark.parametrize("field, value", [("slug", "coach\n"), ("icon", "waveform\n")])
async def test_slug_and_icon_reject_a_trailing_newline(svc, field, value):
    data = {"slug": "coach", field: value}
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", data)
    assert e.value.code == "invalid" and field in e.value.message
    await svc.create("u_a", {"slug": "coach"})
    with pytest.raises(AgentError) as e:
        await svc.update("u_a", "coach", {field: value})
    assert e.value.code == "invalid" and field in e.value.message


async def test_custom_endpoints_allow_only_the_openai_types_by_default():
    svc = await make(config(limits={}))
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "big", "tts": {"type": "tone_tts", "sample_rate": 10**12}})
    assert e.value.code == "invalid"
    assert e.value.message == "tts: type 'tone_tts' is not allowed for custom endpoints on this server"
    stt = {"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w"}
    assert isinstance((await svc.create("u_a", {"slug": "own", "stt": stt})).spec.stt, CustomEndpoint)
    # The operator's own providers of any type keep working.
    assert (await svc.create("u_a", {"slug": "mine", "tts": {"provider": "tts"}})).spec.tts.provider == "tts"


async def test_custom_endpoint_types_are_an_operator_setting():
    svc = await make(config(limits={"custom_endpoint_types": ["tone_tts"]}))
    assert (await svc.create("u_a", {"slug": "tone", "tts": {"type": "tone_tts"}})).spec.tts.type == "tone_tts"
    with pytest.raises(AgentError, match="not allowed"):
        await svc.create("u_a", {"slug": "own", "stt": {"type": "openai_stt", "base_url": "https://s.example/v1", "model": "w"}})


HOOK = {"type": "webhook", "url": "https://hooks.example/notes", "headers": {"Authorization": "Bearer s3cret"}}


async def test_one_way_agents_take_a_webhook_and_no_voice(svc):
    a = await svc.create("u_a", {"slug": "note", "call_type": "one-shot", "action": HOOK})
    assert (a.call_type, a.spec.tts) == ("one-shot", None)
    assert a.spec.stt == ProviderRef(provider="stt")
    shown = agent_detail(a)
    assert shown["tts"] is None
    assert shown["action"] == {"type": "webhook", "url": "https://hooks.example/notes", "headers": {"Authorization": REDACTED}}
    m = await svc.create("u_a", {"slug": "ideas", "call_type": "monologue", "action": HOOK})
    assert m.call_type == "monologue"


async def test_one_way_agent_uses_the_servers_only_webhook_by_default():
    cfg = config(providers={
        "stt": {"type": "fake_stt"}, "llm": {"type": "echo_chat"}, "tts": {"type": "tone_tts"},
        "inbox": {"type": "webhook", "url": "https://n8n.example/webhook/abc"},
    })
    svc = await make(cfg)
    a = await svc.create("u_a", {"slug": "note", "call_type": "one-shot"})
    assert a.spec.action == ProviderRef(provider="inbox")


@pytest.mark.parametrize(
    "action, fragment",
    [
        ({"provider": "llm"}, "is responder, not webhook"),
        ({"type": "webhook", "url": "ftp://x.example"}, "url must be an http(s) URL"),
        ({"type": "webhook", "url": "https://x.example/hook?token=1"}, "put secrets in headers"),
        ({"type": "webhook", "url": "https://user:pw@x.example/hook"}, "put secrets in headers"),
        ({"type": "webhook"}, "missing option 'url'"),
        ({"type": "webhook", "url": "https://x.example", "headers": {"Idempotency-Key": "1"}}, "invalid options"),
        ({"type": "webhook", "url": "https://x.example", "headers": {"X-A": "a\nb"}}, "invalid options"),
    ],
)
async def test_one_way_agent_rejects_bad_webhooks(svc, action, fragment):
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "note", "call_type": "one-shot", "action": action})
    assert e.value.code == "invalid" and fragment in e.value.message
    assert "s3cret" not in e.value.message


async def test_webhook_custom_endpoints_follow_the_operator_limits():
    svc = await make(config(limits={"custom_endpoints": False}))
    with pytest.raises(AgentError) as e:
        await svc.create("u_a", {"slug": "note", "call_type": "one-shot", "action": HOOK})
    assert "custom endpoints are off" in e.value.message


async def test_switching_call_type_keeps_or_drops_the_voice(svc):
    a = await svc.create("u_a", {"slug": "helper"})
    one = await svc.update("u_a", "helper", {"call_type": "one-shot", "action": HOOK})
    # The voice stays stored, so switching back needs only the responder.
    assert one.spec.tts == ProviderRef(provider="tts")
    back = await svc.update("u_a", "helper", {"call_type": "conversation", "action": {"provider": "llm"}})
    assert back.spec.tts == a.spec.tts
    await svc.update("u_a", "helper", {"call_type": "one-shot", "action": HOOK})
    silent = await svc.update("u_a", "helper", {"tts": None})
    assert silent.spec.tts is None
    with pytest.raises(AgentError) as e:
        await svc.update("u_a", "helper", {"call_type": "conversation", "action": {"provider": "llm"}})
    assert e.value.message == "tts: required for conversation agents"


async def test_every_webhook_header_value_is_redacted_and_round_trips(svc):
    hook = {**HOOK, "headers": {"X-Signature": "sig", "X-N8N-Header": "n8n", "Authorization": "Bearer s3cret"}}
    await svc.create("u_a", {"slug": "note", "call_type": "one-shot", "action": hook})
    shown = agent_detail(await svc.get("u_a", "note"))
    assert shown["action"]["headers"] == {"X-Signature": REDACTED, "X-N8N-Header": REDACTED, "Authorization": REDACTED}
    shown["action"]["headers"]["X-New"] = "plain"
    again = await svc.update("u_a", "note", {"action": shown["action"]})
    assert again.spec.action.options()["headers"] == {**hook["headers"], "X-New": "plain"}


async def test_redacted_webhook_header_round_trips(svc):
    await svc.create("u_a", {"slug": "note", "call_type": "one-shot", "action": HOOK})
    shown = agent_detail(await svc.get("u_a", "note"))
    again = await svc.update("u_a", "note", {"action": shown["action"]})
    assert again.spec.action.options()["headers"] == {"Authorization": "Bearer s3cret"}


async def test_conversation_providers_need_a_voice():
    cfg = config()
    spec = AgentSpec(stt=ProviderRef(provider="stt"), action=ProviderRef(provider="llm"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderError, match="needs a tts"):
            build_agent_providers(cfg, spec, http)
