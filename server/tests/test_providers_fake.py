import httpx
import pytest

from conftest import fake_config
from wristcall.agents import build_agent_providers, spec_from_profile
from wristcall.config import ProviderConfig, parse_config
from wristcall.providers import ProviderError, auth_headers, build_provider, check_providers
from wristcall.providers.fake import EchoChat, FakeStt, ToneTts


async def collect(agen):
    return [x async for x in agen]


async def test_build_providers_from_config():
    cfg = fake_config()
    async with httpx.AsyncClient() as http:
        ps = build_agent_providers(cfg, spec_from_profile(cfg.profiles["default"]), http)
    assert isinstance(ps.stt, FakeStt) and isinstance(ps.responder, EchoChat) and isinstance(ps.tts, ToneTts)


async def test_fake_providers_behaviour():
    cfg = fake_config()
    async with httpx.AsyncClient() as http:
        ps = build_agent_providers(cfg, spec_from_profile(cfg.profiles["default"]), http)
    assert await ps.stt.transcribe(b"RIFF", "en") == "hi"
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "all good"}]
    assert "".join(await collect(ps.responder.respond(msgs))) == "You said: all good"
    chunks = await collect(ps.tts.synthesize("abcdefghij"))
    assert ps.tts.sample_rate == 16000
    assert sum(len(c) for c in chunks) == 16000 * 100 // 1000 * 2
    assert all(len(c) % 2 == 0 for c in chunks)


async def test_unknown_type_and_wrong_kind():
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderError, match="unknown type"):
            build_provider("x", ProviderConfig(type="does_not_exist"), "stt", http)
        with pytest.raises(ProviderError, match="used as stt"):
            build_provider("x", ProviderConfig(type="tone_tts"), "stt", http)


@pytest.mark.parametrize("opts", [{"sample_rate": 0}, {"sample_rate": -1}, {"ms_per_char": -1}])
async def test_tone_tts_rejects_invalid_options(opts):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderError, match="sample_rate|ms_per_char"):
            build_provider("t", ProviderConfig(type="tone_tts", **opts), "tts", http)


def test_auth_headers():
    assert auth_headers(None) == {}
    assert auth_headers("") == {}
    assert auth_headers("k") == {"Authorization": "Bearer k"}


async def test_check_providers_fails_on_any_broken_provider():
    def cfg(providers):
        return parse_config({"server": {"public_url": "https://wc.test"}, "providers": providers}, {})

    async with httpx.AsyncClient() as http:
        check_providers(cfg({"stt": {"type": "fake_stt"}}), http)
        with pytest.raises(ProviderError, match="unknown type 'nope'"):
            check_providers(cfg({"x": {"type": "nope"}}), http)
        with pytest.raises(ProviderError, match="unused"):
            check_providers(cfg({"unused": {"type": "openai_stt"}}), http)  # missing base_url, even if no agent uses it
