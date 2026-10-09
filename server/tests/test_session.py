import asyncio

import pytest

from conftest import fake_config
from wristcall.audio import FRAME_BYTES_IN
from wristcall.providers import ProviderError, ProviderSet
from wristcall.session import CallSession
from wristcall.turn import State

SPEECH = b"\x01" * FRAME_BYTES_IN
SIL = b"\x00" * FRAME_BYTES_IN


class ByteVad:
    def is_speech(self, frame: bytes) -> bool:
        return frame[0] != 0

    def reset(self) -> None:
        pass


class FakeTransport:
    def __init__(self) -> None:
        self.events: list = []

    async def send_json(self, msg: dict) -> None:
        self.events.append(msg)

    async def send_bytes(self, data: bytes) -> None:
        self.events.append(data)

    def types(self) -> list[str]:
        return [e["type"] if isinstance(e, dict) else "audio" for e in self.events]

    def audio(self) -> list[bytes]:
        return [e for e in self.events if isinstance(e, bytes)]

    def errors(self) -> list[dict]:
        return [e for e in self.events if isinstance(e, dict) and e["type"] == "error"]


class Stt:
    def __init__(self, text="hi", exc=None, delay=0.0):
        self.text, self.exc, self.delay = text, exc, delay
        self.calls = 0

    async def transcribe(self, wav, language):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.text


class Llm:
    def __init__(self, pieces=("Hello. ", "How are you?"), fail_after=None, hang=False):
        self.pieces, self.fail_after, self.hang = pieces, fail_after, hang
        self.seen: list = []

    async def respond(self, messages):
        self.seen.append([dict(m) for m in messages])
        if self.hang:
            await asyncio.sleep(3600)
        for i, piece in enumerate(self.pieces):
            if self.fail_after is not None and i == self.fail_after:
                raise ProviderError("dropped")
            yield piece


class Tts:
    sample_rate = 16000

    def __init__(self, fail=False, ms_per_call=100):
        self.fail, self.ms = fail, ms_per_call
        self.texts: list[str] = []

    async def synthesize(self, text):
        self.texts.append(text)
        if self.fail:
            raise ProviderError("tts down")
        n = self.sample_rate * self.ms // 1000 * 2
        yield b"\x05" * (n - 100)
        yield b"\x05" * 100


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make(stt=None, llm=None, tts=None, sleep=None, turn_end=None, **profile_over):
    cfg = fake_config(**profile_over)
    profile = cfg.profiles["default"]
    transport = FakeTransport()
    sleeps = sleep or Sleeps()
    extra = {} if turn_end is None else {"turn_end": turn_end}
    s = CallSession(profile, ProviderSet(stt or Stt(), llm or Llm(), tts or Tts()), ByteVad(), transport, sleep=sleeps, **extra)
    return s, transport, sleeps


async def speak(s: CallSession, speech_frames=25, silence_frames=40):
    for _ in range(speech_frames):
        await s.on_audio(SPEECH)
    for _ in range(silence_frames):
        await s.on_audio(SIL)


async def test_full_turn_happy_path():
    s, t, sleeps = make()
    await speak(s)
    await s.wait_idle()
    assert t.types()[0] == "turn.user_end" and t.events[0]["reason"] == "vad"
    assert t.events[1] == {"type": "transcript", "role": "user", "text": "hi"}
    assert t.events[2] == {"type": "turn.agent_start"}
    audio = t.audio()
    assert audio and all(len(f) == 640 for f in audio)
    assert len(audio) == 10
    assert t.types()[-2:] == ["transcript", "turn.agent_end"]
    assert t.events[-2] == {"type": "transcript", "role": "assistant", "text": "Hello. How are you?"}
    assert s.history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello. How are you?"},
    ]
    assert s.machine.state is State.LISTENING
    assert len(sleeps.calls) == 1 and 0.2 <= sleeps.calls[0] <= 0.4


async def test_waits_for_playback_while_still_speaking():
    seen = []
    holder = {}

    async def sleep(seconds):
        seen.append((holder["s"].machine.state, seconds))

    s, _, _ = make(tts=Tts(ms_per_call=1000), sleep=sleep)
    holder["s"] = s
    await speak(s)
    await s.wait_idle()
    assert seen[0][0] is State.SPEAKING
    assert 2.0 <= seen[0][1] <= 2.2
    assert s.machine.state is State.LISTENING


async def test_messages_include_system_prompt_and_history():
    llm = Llm()
    s, _, _ = make(llm=llm)
    await speak(s)
    await s.wait_idle()
    await speak(s)
    await s.wait_idle()
    assert llm.seen[1] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello. How are you?"},
        {"role": "user", "content": "hi"},
    ]


async def test_audio_during_agent_turn_is_discarded():
    stt = Stt(delay=0.05)
    s, _, _ = make(stt=stt)
    await speak(s)
    await speak(s)
    await s.wait_idle()
    assert stt.calls == 1


async def test_arbitrary_chunk_sizes_are_reframed():
    s, t, _ = make()
    blob = SPEECH * 25 + SIL * 40
    for i in range(0, len(blob), 1000):
        await s.on_audio(blob[i : i + 1000])
    await s.wait_idle()
    assert "turn.user_end" in t.types()


async def test_mute_closes_turn():
    s, t, _ = make()
    for _ in range(25):
        await s.on_audio(SPEECH)
    await s.on_mute(True)
    await s.wait_idle()
    assert t.events[0] == {"type": "turn.user_end", "reason": "mute"}
    assert "turn.agent_end" in t.types()


async def test_empty_transcript_returns_to_listening_silently():
    s, t, _ = make(stt=Stt(text="  "))
    await speak(s)
    await s.wait_idle()
    assert t.types() == ["turn.user_end"]
    assert s.machine.state is State.LISTENING


async def test_stt_failure_is_not_fatal():
    s, t, _ = make(stt=Stt(exc=ProviderError("500")))
    await speak(s)
    await s.wait_idle()
    assert t.errors() == [{"type": "error", "code": "stt_failed", "message": "I could not understand the audio.", "fatal": False}]
    assert s.machine.state is State.LISTENING


async def test_stt_timeout_uses_profile_value():
    s, t, _ = make(stt=Stt(delay=0.5), timeouts={"stt_s": 0.05})
    await speak(s)
    await s.wait_idle()
    assert [e["code"] for e in t.errors()] == ["stt_failed"]


async def test_responder_without_text_speaks_fallback():
    tts = Tts()
    s, t, _ = make(llm=Llm(fail_after=0), tts=tts)
    await speak(s)
    await s.wait_idle()
    assert [e["code"] for e in t.errors()] == ["responder_failed"]
    assert tts.texts == ["Sorry, I couldn't answer right now."]
    assert s.history == [{"role": "user", "content": "hi"}]
    assert s.machine.state is State.LISTENING


@pytest.mark.parametrize("pieces", [(), ("  ",)], ids=["empty", "only-spaces"])
async def test_responder_empty_answer_speaks_fallback(pieces):
    tts = Tts()
    s, t, _ = make(llm=Llm(pieces=pieces), tts=tts)
    await speak(s)
    await s.wait_idle()
    assert t.errors() == [
        {"type": "error", "code": "responder_failed", "message": "I could not generate a response.", "fatal": False}
    ]
    assert tts.texts == ["Sorry, I couldn't answer right now."]
    assert s.history == [{"role": "user", "content": "hi"}]
    assert {"type": "transcript", "role": "assistant", "text": ""} not in t.events
    assert "turn.agent_end" in t.types()
    assert s.machine.state is State.LISTENING


async def test_responder_first_token_timeout():
    s, t, _ = make(llm=Llm(hang=True), timeouts={"first_token_s": 0.05})
    await speak(s)
    await s.wait_idle()
    assert [e["code"] for e in t.errors()] == ["responder_failed"]


async def test_responder_failure_mid_stream_keeps_spoken_part():
    tts = Tts()
    s, t, _ = make(llm=Llm(pieces=("Hello. ", "rest"), fail_after=1), tts=tts)
    await speak(s)
    await s.wait_idle()
    assert tts.texts == ["Hello."]
    assert [e["code"] for e in t.errors()] == ["responder_failed"]
    assert s.history[-1] == {"role": "assistant", "content": "Hello."}


async def test_tts_failure_still_sends_transcript():
    s, t, _ = make(tts=Tts(fail=True))
    await speak(s)
    await s.wait_idle()
    assert [e["code"] for e in t.errors()] == ["tts_failed"]
    assert {"type": "transcript", "role": "assistant", "text": "Hello. How are you?"} in t.events
    assert t.audio() == []
    assert s.machine.state is State.LISTENING


async def test_close_cancels_running_turn():
    s, _, _ = make(llm=Llm(hang=True))
    await speak(s)
    await asyncio.sleep(0.01)
    await s.close()
    assert s.machine.state is State.ENDED
    await speak(s)
    await s.wait_idle()


class OddTts(Tts):
    """Generates audio that is not a multiple of 20 ms (1000 bytes = 1 frame and 360 bytes of remainder)."""

    async def synthesize(self, text):
        self.texts.append(text)
        yield b"\x05" * 1000


async def test_padded_last_frame_comes_before_assistant_transcript():
    s, t, _ = make(tts=OddTts())
    await speak(s)
    await s.wait_idle()
    audio = t.audio()
    assert all(len(f) == 640 for f in audio)
    assert len(audio) == 4
    assert audio[-1] == b"\x05" * 80 + b"\x00" * 560
    types = t.types()
    last_audio = max(i for i, k in enumerate(types) if k == "audio")
    assert last_audio < t.events.index({"type": "transcript", "role": "assistant", "text": "Hello. How are you?"})
    assert types[-2:] == ["transcript", "turn.agent_end"]


class BrokenTts(Tts):
    async def synthesize(self, text):
        self.texts.append(text)
        yield b"\x05" * 1000
        raise RuntimeError("bug")


async def test_unexpected_error_while_speaking_ends_turn_and_waits_playback():
    s, t, sleeps = make(tts=BrokenTts())
    await speak(s)
    await s.wait_idle()
    types = t.types()
    assert "turn.agent_end" in types
    assert types.index("turn.agent_end") > max(i for i, k in enumerate(types) if k == "audio")
    assert [e["code"] for e in t.errors()] == ["internal"]
    assert t.errors()[0]["fatal"] is False
    assert len(sleeps.calls) == 1
    assert s.machine.state is State.LISTENING


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class GapTts(Tts):
    """1 s of audio per sentence; before the second sentence the clock advances 1.5 s (the watch has already played the first)."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(ms_per_call=1000)
        self.clock = clock

    async def synthesize(self, text):
        if self.texts:
            self.clock.now += 1.5
        async for chunk in super().synthesize(text):
            yield chunk


async def test_playback_wait_accounts_for_gaps_between_sentences():
    clock = FakeClock()
    sleeps = Sleeps()
    transport = FakeTransport()
    profile = fake_config().profiles["default"]
    s = CallSession(
        profile, ProviderSet(Stt(), Llm(), GapTts(clock)), ByteVad(), transport, sleep=sleeps, clock=clock
    )
    await speak(s)
    await s.wait_idle()
    # Sentence 1 plays from 0 to 1 s; sentence 2 arrives at 1.5 s and plays until 2.5 s. At the end, the clock is at 1.5 s.
    assert len(transport.audio()) == 100
    assert clock.now == 1.5
    assert sleeps.calls == [pytest.approx(1.0 + 0.2, abs=1e-6)]
    assert s.machine.state is State.LISTENING


async def test_session_defaults_to_auto_turn_end():
    s, _, _ = make()
    assert s.machine.turn_end == "auto"


async def test_manual_call_ends_turn_only_on_mute():
    stt = Stt()
    s, t, _ = make(stt=stt, turn_end="manual")
    await speak(s, speech_frames=25, silence_frames=500)
    await s.wait_idle()
    assert t.events == [] and stt.calls == 0
    await s.on_mute(True)
    await s.wait_idle()
    assert t.events[0] == {"type": "turn.user_end", "reason": "mute"}
    assert "turn.agent_end" in t.types() and stt.calls == 1


class Recorded:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    async def __call__(self, role, text, error) -> None:
        self.rows.append((role, text, error))


def recorded(**kw):
    rec = Recorded()
    s, t, _ = make(**kw)
    s._record = rec
    return s, t, rec


async def test_each_turn_is_recorded_user_then_agent():
    s, _, rec = recorded()
    await speak(s)
    await s.wait_idle()
    await speak(s)
    await s.wait_idle()
    assert rec.rows == [("user", "hi", None), ("agent", "Hello. How are you?", None)] * 2


async def test_stt_failure_is_recorded_without_text():
    s, _, rec = recorded(stt=Stt(exc=ProviderError("down")))
    await speak(s)
    await s.wait_idle()
    assert rec.rows == [("user", None, "stt_failed")]


async def test_blank_transcript_records_nothing():
    s, _, rec = recorded(stt=Stt(text="  "))
    await speak(s)
    await s.wait_idle()
    assert rec.rows == []


async def test_responder_failure_is_recorded_with_what_was_said():
    s, _, rec = recorded(llm=Llm(fail_after=1))
    await speak(s)
    await s.wait_idle()
    assert rec.rows == [("user", "hi", None), ("agent", "Hello.", "responder_failed")]


async def test_empty_response_is_recorded_as_a_failure():
    s, _, rec = recorded(llm=Llm(pieces=()))
    await speak(s)
    await s.wait_idle()
    assert rec.rows == [("user", "hi", None), ("agent", None, "responder_failed")]


async def test_tts_failure_keeps_the_answer_text():
    s, _, rec = recorded(tts=Tts(fail=True))
    await speak(s)
    await s.wait_idle()
    assert rec.rows == [("user", "hi", None), ("agent", "Hello. How are you?", "tts_failed")]
