import asyncio
import concurrent.futures
import socket
import threading
import time
import wave
from pathlib import Path

import pytest
import uvicorn

from refclient.client import CallError, call_url, pair, run_call, wav_source
from wristcall.app import create_app
from wristcall.config import parse_config
from wristcall.providers import ProviderError, register

FIXTURE = Path(__file__).resolve().parents[3] / "server" / "tests" / "fixtures" / "speech_pt_16k.wav"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@register("failing_stt", "stt")
class FailingStt:
    """STT that always fails: the server answers error{stt_failed} and never reaches turn.agent_end."""

    def __init__(self, options, http) -> None:
        pass

    async def transcribe(self, wav: bytes, language: str) -> str:
        raise ProviderError("STT is down (test)")


def _sync(coro):
    # Works from sync tests and from inside a running event loop (async tests): a fresh loop on another thread.
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


class SyncPairing:
    """The server's pairing service, driven from the test thread, acting for the `owner` user."""

    def __init__(self, app) -> None:
        self._pairing = app.state.pairing
        self._storage = app.state.storage

    def _owner(self) -> str:
        return _sync(self._storage.users.by_handle("owner")).id

    def create_code(self):
        return _sync(self._pairing.create_code(self._owner()))

    def list_pending(self):
        return _sync(self._pairing.list_pending())

    def approve(self, request_id: str) -> str:
        return _sync(self._pairing.approve(request_id, self._owner()))

    def authenticate(self, token: str):
        return _sync(self._pairing.authenticate(token))


def start_server(tmp_path, approval="code", stt=None):
    cfg = parse_config(
        {
            "server": {"public_url": "http://127.0.0.1", "data_dir": str(tmp_path), "pairing_approval": approval},
            "providers": {
                "stt": stt or {"type": "fake_stt", "text": "hi"},
                "llm": {"type": "echo_chat"},
                "tts": {"type": "tone_tts", "sample_rate": 16000},
            },
            "profiles": {"default": {"display_name": "Test", "stt": "stt", "responder": "llm", "tts": "tts", "vad": {"type": "energy"}}},
        },
        {},
    )
    app = create_app(cfg)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("server did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", SyncPairing(app), server, thread


@pytest.fixture
def running(tmp_path):
    base, svc, server, thread = start_server(tmp_path)
    yield base, svc
    server.should_exit = True
    thread.join(5)


def test_call_url():
    assert call_url("https://wc.example.test/") == "wss://wc.example.test/v1/call"
    assert call_url("http://127.0.0.1:8080") == "ws://127.0.0.1:8080/v1/call"
    with pytest.raises(ValueError):
        call_url("wc.example.test")


async def test_pair_and_call_with_recorded_speech(running):
    base, svc = running
    creds = pair(base, svc.create_code().code, "refclient-test")
    with wave.open(str(FIXTURE)) as w:
        pcm = w.readframes(w.getnframes())
    result = await run_call(base, creds["token"], wav_source(pcm, realtime=False), stop_after_agent_turns=1)
    transcripts = [(e["role"], e["text"]) for e in result.events if e["type"] == "transcript"]
    assert transcripts == [("user", "hi"), ("assistant", "You said: hi")]
    assert result.sample_rate == 16000
    assert len(result.audio) > 0 and len(result.audio) % 640 == 0


async def test_sender_failure_becomes_call_error(running):
    base, svc = running
    creds = pair(base, svc.create_code().code, "refclient-failure")

    async def broken_source():
        for _ in range(3):
            yield b"\x00" * 640
            await asyncio.sleep(0.02)
        raise RuntimeError("microphone broke")

    with pytest.raises(CallError, match="failed to send audio"):
        await asyncio.wait_for(run_call(base, creds["token"], broken_source()), timeout=10)


async def test_revoked_token_becomes_call_error(running):
    base, _ = running
    with pytest.raises(CallError, match="token"):
        await asyncio.wait_for(run_call(base, "x", wav_source(b"", realtime=False)), timeout=10)


async def test_stop_on_error_ends_call_when_stt_fails(tmp_path):
    base, svc, server, thread = start_server(tmp_path, stt={"type": "failing_stt"})
    try:
        creds = pair(base, svc.create_code().code, "refclient-stt")
        with wave.open(str(FIXTURE)) as w:
            pcm = w.readframes(w.getnframes())
        result = await asyncio.wait_for(
            run_call(base, creds["token"], wav_source(pcm, realtime=False), stop_after_agent_turns=1, stop_on_error=True),
            timeout=10,
        )
        errors = [e for e in result.events if e["type"] == "error"]
        assert errors == [{"type": "error", "code": "stt_failed", "message": "I could not understand the audio.", "fatal": False}]
        assert not any(e["type"] == "turn.agent_end" for e in result.events)
    finally:
        server.should_exit = True
        thread.join(5)


def test_pair_flow_b_waits_for_approval(tmp_path):
    base, svc, server, thread = start_server(tmp_path, approval="manual")
    try:
        def approve_soon():
            for _ in range(100):
                pending = svc.list_pending()
                if pending:
                    svc.approve(pending[0].request_id)
                    return
                time.sleep(0.05)

        threading.Thread(target=approve_soon, daemon=True).start()
        creds = pair(base, None, "refclient-b", poll_interval_s=0.05, timeout_s=10)
        assert svc.authenticate(creds["token"]).name == "refclient-b"
    finally:
        server.should_exit = True
        thread.join(5)
