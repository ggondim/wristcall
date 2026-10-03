import asyncio
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from wristcall.app import create_app
from wristcall.config import ConfigError, WarmupConfig, parse_config
from wristcall.pairing import PairingService
from wristcall.providers import provider_kind
from wristcall.store import Database
from wristcall.warmup import WarmupTarget, run_background, warm_one, warmup_targets

START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}


def config(tts_warmup=None, stt_warmup=None, llm_warmup=None):
    def with_warmup(provider, warmup):
        return {**provider, "warmup": warmup} if warmup is not None else provider

    return parse_config(
        {
            "server": {"public_url": "http://testserver"},
            "providers": {
                "stt": with_warmup({"type": "fake_stt"}, stt_warmup),
                "llm": with_warmup({"type": "echo_chat"}, llm_warmup),
                "tts": with_warmup({"type": "openai_tts", "base_url": "http://tts.test/v1", "model": "t"}, tts_warmup),
            },
            "profiles": {"default": {"display_name": "Test", "stt": "stt", "responder": "llm", "tts": "tts", "vad": {"type": "energy"}}},
        },
        {},
    )


class CountingStt:
    def __init__(self, fail=False):
        self.calls: list[tuple[bytes, str]] = []
        self.fail = fail

    async def transcribe(self, wav, language):
        self.calls.append((wav, language))
        if self.fail:
            raise RuntimeError("down")
        return ""


class CountingTts:
    sample_rate = 16000

    def __init__(self):
        self.texts: list[str] = []
        self.consumed = 0

    async def synthesize(self, text):
        self.texts.append(text)
        for _ in range(3):
            self.consumed += 1
            yield b"\x00\x00"


def test_warmup_config_is_separate_from_provider_options():
    cfg = config(tts_warmup={"on_call": True})
    p = cfg.providers["tts"]
    assert p.warmup == WarmupConfig(on_call=True)
    assert "warmup" not in p.options()
    assert cfg.providers["stt"].warmup is None
    with pytest.raises(ConfigError):
        config(tts_warmup={"on_boot": True})


def test_provider_kind():
    assert provider_kind("openai_tts") == "tts"
    assert provider_kind("fake_stt") == "stt"
    assert provider_kind("does_not_exist") is None


async def test_targets_only_for_stt_and_tts():
    cfg = config(tts_warmup={"on_start": True}, stt_warmup={"on_call": True}, llm_warmup={"on_start": True})
    async with httpx.AsyncClient() as http:
        targets = warmup_targets(cfg, http)
    assert sorted((t.name, t.kind) for t in targets) == [("stt", "stt"), ("tts", "tts")]


async def test_warm_one_stt_and_tts():
    stt, tts = CountingStt(), CountingTts()
    assert await warm_one(WarmupTarget("s", "stt", stt, WarmupConfig(language="en")))
    assert stt.calls[0][0][:4] == b"RIFF" and stt.calls[0][1] == "en"
    assert await warm_one(WarmupTarget("t", "tts", tts, WarmupConfig(text="Hi.")))
    assert tts.texts == ["Hi."] and tts.consumed == 3


async def test_warm_one_failure_does_not_raise():
    assert await warm_one(WarmupTarget("s", "stt", CountingStt(fail=True), WarmupConfig())) is False


async def test_run_background_start_then_periodic():
    stt = CountingStt()
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    target = WarmupTarget("s", "stt", stt, WarmupConfig(on_start=True, every_s=240))
    with pytest.raises(asyncio.CancelledError):
        await run_background([target], sleep=sleep)
    assert sleeps == [240, 240, 240]
    assert len(stt.calls) == 3


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_app_warms_on_start():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://tts.test/v1/audio/speech").mock(return_value=httpx.Response(200, content=b"\x00\x00"))
        app = create_app(config(tts_warmup={"on_start": True}), pairing=PairingService(Database(":memory:")))
        with TestClient(app):
            assert wait_until(lambda: route.called)
        assert json.loads(route.calls.last.request.content)["input"] == "Hello."


def test_app_warms_on_call():
    svc = PairingService(Database(":memory:"))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://tts.test/v1/audio/speech").mock(return_value=httpx.Response(200, content=b"\x00\x00"))
        with TestClient(create_app(config(tts_warmup={"on_call": True}), pairing=svc)) as client:
            time.sleep(0.1)
            assert not route.called
            token = client.post("/v1/pair", json={"code": svc.create_code().code, "device_name": "w"}).json()["token"]
            with client.websocket_connect("/v1/call", headers={"Authorization": f"Bearer {token}"}) as ws:
                ws.send_json(START)
                assert ws.receive_json()["type"] == "session.ready"
                assert wait_until(lambda: route.called)
                ws.send_json({"type": "session.end"})
