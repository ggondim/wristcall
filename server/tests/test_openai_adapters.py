import json

import httpx
import pytest
import respx

from wristcall.config import ProviderConfig
from wristcall.providers import ProviderError, build_provider


def make(kind, **opts):
    return build_provider("p", ProviderConfig(**opts), kind, httpx.AsyncClient())


async def collect(agen):
    return [x async for x in agen]


# ---------- STT ----------

STT = {"type": "openai_stt", "base_url": "http://stt/v1/", "model": "Systran/faster-whisper-large-v3"}


@respx.mock
async def test_stt_sends_multipart_and_returns_text():
    route = respx.post("http://stt/v1/audio/transcriptions").mock(return_value=httpx.Response(200, json={"text": "  hello world "}))
    stt = make("stt", **STT, api_key="k", extra_form={"vad_filter": False})
    assert await stt.transcribe(b"RIFFxxxx", "en") == "hello world"
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer k"
    body = req.content
    assert b'name="model"\r\n\r\nSystran/faster-whisper-large-v3' in body
    assert b'name="language"\r\n\r\nen' in body
    assert b'name="vad_filter"\r\n\r\nfalse' in body
    assert b'filename="turn.wav"' in body


@respx.mock
async def test_stt_http_error():
    respx.post("http://stt/v1/audio/transcriptions").mock(return_value=httpx.Response(500, text="No clip timestamps found"))
    with pytest.raises(ProviderError, match="500"):
        await make("stt", **STT).transcribe(b"RIFF", "en")


@respx.mock
async def test_stt_connection_error():
    respx.post("http://stt/v1/audio/transcriptions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderError, match="unreachable"):
        await make("stt", **STT).transcribe(b"RIFF", "en")


def test_stt_requires_model():
    with pytest.raises(ProviderError, match="model"):
        make("stt", type="openai_stt", base_url="http://stt/v1")


# ---------- chat ----------

CHAT = {"type": "openai_chat", "base_url": "http://llm/v1", "model": "m", "api_key": "vk", "extra_body": {"temperature": 0.3}}


def sse(*events: str) -> bytes:
    return "".join(f"data: {e}\n\n" for e in events).encode()


@respx.mock
async def test_chat_streams_deltas():
    body = sse(
        json.dumps({"choices": [{"delta": {"role": "assistant"}}]}),
        json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
        "this is not json",
        json.dumps({"choices": [{"delta": {"content": ", world"}}]}),
        json.dumps({"choices": [], "usage": {"total_tokens": 9}}),
        "[DONE]",
    )
    route = respx.post("http://llm/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    chat = make("responder", **CHAT)
    msgs = [{"role": "user", "content": "hi"}]
    assert "".join(await collect(chat.respond(msgs))) == "Hello, world"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"model": "m", "messages": msgs, "stream": True, "temperature": 0.3}
    assert route.calls.last.request.headers["Authorization"] == "Bearer vk"


@respx.mock
async def test_chat_http_error():
    respx.post("http://llm/v1/chat/completions").mock(return_value=httpx.Response(401, text="invalid key"))
    with pytest.raises(ProviderError, match="401"):
        await collect(make("responder", **CHAT).respond([{"role": "user", "content": "hi"}]))


# ---------- TTS ----------

TTS = {"type": "openai_tts", "base_url": "http://tts/v1", "model": "tts-1-hd", "voice": "alloy"}


@respx.mock
async def test_tts_streams_even_pcm_chunks():
    async def odd_chunks():
        yield b"\x01\x02\x03"
        yield b"\x04\x05\x06"

    route = respx.post("http://tts/v1/audio/speech").mock(return_value=httpx.Response(200, content=odd_chunks()))
    tts = make("tts", **TTS)
    assert tts.sample_rate == 24000
    chunks = await collect(tts.synthesize("Hello."))
    assert all(len(c) % 2 == 0 for c in chunks)
    assert b"".join(chunks) == b"\x01\x02\x03\x04\x05\x06"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"model": "tts-1-hd", "voice": "alloy", "input": "Hello.", "response_format": "pcm"}


def test_tts_sample_rate_is_configurable():
    assert make("tts", **TTS, sample_rate=22050).sample_rate == 22050


@respx.mock
async def test_tts_http_error():
    respx.post("http://tts/v1/audio/speech").mock(return_value=httpx.Response(500, text="failed"))
    with pytest.raises(ProviderError, match="500"):
        await collect(make("tts", **TTS).synthesize("Hello."))


# ---------- round 1 fixes ----------


@respx.mock
async def test_stt_non_json_body_is_provider_error():
    respx.post("http://stt/v1/audio/transcriptions").mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(ProviderError, match="invalid"):
        await make("stt", **STT).transcribe(b"RIFF", "en")


@respx.mock
async def test_stt_non_object_json_is_provider_error():
    respx.post("http://stt/v1/audio/transcriptions").mock(return_value=httpx.Response(200, json=["x"]))
    with pytest.raises(ProviderError, match="invalid"):
        await make("stt", **STT).transcribe(b"RIFF", "en")


@respx.mock
async def test_chat_midstream_error_event_is_provider_error():
    body = sse(
        json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
        json.dumps({"error": {"message": "upstream"}}),
    )
    respx.post("http://llm/v1/chat/completions").mock(return_value=httpx.Response(200, content=body))
    got = []
    with pytest.raises(ProviderError, match="upstream"):
        async for piece in make("responder", **CHAT).respond([{"role": "user", "content": "hi"}]):
            got.append(piece)
    assert got == ["Hello"]


@respx.mock
async def test_chat_ignores_non_object_json_chunks():
    body = sse("1", json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "[DONE]")
    respx.post("http://llm/v1/chat/completions").mock(return_value=httpx.Response(200, content=body))
    assert await collect(make("responder", **CHAT).respond([{"role": "user", "content": "hi"}])) == ["ok"]


@respx.mock
async def test_tts_null_voice_falls_back_to_alloy():
    route = respx.post("http://tts/v1/audio/speech").mock(return_value=httpx.Response(200, content=b"\x01\x02"))
    await collect(make("tts", **{**TTS, "voice": None}).synthesize("Hello."))
    assert json.loads(route.calls.last.request.content)["voice"] == "alloy"


# ---------- final review ----------


@respx.mock
async def test_stt_null_text_is_empty_string():
    respx.post("http://stt/v1/audio/transcriptions").mock(return_value=httpx.Response(200, json={"text": None}))
    assert await make("stt", **STT).transcribe(b"RIFF", "en") == ""


@pytest.mark.parametrize("rate", [0, -16000])
def test_tts_rejects_non_positive_sample_rate(rate):
    with pytest.raises(ProviderError, match="sample_rate"):
        make("tts", **TTS, sample_rate=rate)
