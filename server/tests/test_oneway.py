import asyncio

import pytest

from memory_storage import MemoryStorage
from wristcall.agents import Agent, AgentSpec, OneWayProviders, ProviderRef
from wristcall.audio import FRAME_BYTES_IN
from wristcall.config import VadConfig
from wristcall.delivery import DeliveryPolicy
from wristcall.history import CallLog
from wristcall.history_codec import HistoryCodec
from wristcall.oneway import Background, OneWayCall, new_call_id
from wristcall.providers import ProviderError
from wristcall.providers.webhook import WebhookError
from wristcall.storage import CallRecord

SPEECH = b"\x01" * FRAME_BYTES_IN
SIL = b"\x00" * FRAME_BYTES_IN


class ByteVad:
    def is_speech(self, frame: bytes) -> bool:
        return frame[0] != 0

    def reset(self) -> None:
        pass


class Stt:
    """Answers each segment in order; an exception in `answers` fails that try."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.lengths: list[int] = []

    async def transcribe(self, wav, language):
        self.lengths.append(len(wav))
        answer = self.answers.pop(0) if self.answers else "x"
        if isinstance(answer, Exception):
            raise answer
        return answer


class Hook:
    def __init__(self, *answers):
        self.answers = list(answers) or [204]
        self.bodies: list[dict] = []
        self.keys: list[str] = []

    async def send(self, body, *, idempotency_key, timeout_s):
        self.bodies.append(body)
        self.keys.append(idempotency_key)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def no_sleep(_s):
    pass


def agent(call_type="one-shot", **vad) -> Agent:
    spec = AgentSpec(
        language="pt",
        stt=ProviderRef(provider="stt"),
        action=ProviderRef(provider="hook"),
        vad=VadConfig(type="energy", silence_ms=100, min_speech_ms=40, max_turn_ms=1000, pre_roll_ms=0, **vad),
    )
    return Agent(
        id="ag_1", user_id="u_a", slug="note", display_name="Note", icon="waveform", call_type=call_type,
        position=0, spec=spec, created_at=1.0, updated_at=1.0,
    )


async def make(call_type="one-shot", stt=None, hook=None, max_call_ms=60_000, **vad):
    st = MemoryStorage()
    await st.users.create("u_a", "alice", "Alice", 1.0)
    record = await st.calls.create(CallRecord(
        id=new_call_id(), user_id="u_a", agent_id="ag_1", device_id="d_1", call_type=call_type,
        status="recording", created_at=100.0, updated_at=100.0,
    ))
    clock = iter(range(200, 10_000))
    call = OneWayCall(
        agent(call_type, **vad), OneWayProviders(stt=stt or Stt("olá"), webhook=hook or Hook()), ByteVad(),
        CallLog(st, HistoryCodec(), record),
        max_call_ms=max_call_ms, policy=DeliveryPolicy(1.0, (0.0, 0.0)), now=lambda: float(next(clock)), sleep=no_sleep,
    )
    return call, st


def frames(*parts: tuple[bytes, int]) -> bytes:
    return b"".join(frame * n for frame, n in parts)


def test_new_call_ids_are_unique_and_prefixed():
    ids = {new_call_id() for _ in range(100)}
    assert len(ids) == 100 and all(i.startswith("c_") and len(i) == 18 for i in ids)


async def test_one_shot_keeps_pauses_and_delivers_at_hang_up():
    stt, hook = Stt("comprar leite"), Hook(204)
    call, st = await make(stt=stt, hook=hook)
    # Silence far longer than silence_ms does not end a one-shot.
    call.on_audio(frames((SPEECH, 5), (SIL, 20), (SPEECH, 5)))
    assert not call.captured
    done = await call.finish()
    assert (done.status, call.text, done.attempts, done.last_http_status, done.error) == (
        "delivered", "comprar leite", 1, 204, None
    )
    assert done.ended_at == 200.0 and done.finished_at is not None
    assert stt.lengths == [44 + 30 * FRAME_BYTES_IN]  # one segment, pauses included (WAV header is 44 bytes)
    body = hook.bodies[0]
    assert (body["call_id"], body["call_type"], body["text"], body["language"]) == (call.record.id, "one-shot", "comprar leite", "pt")
    assert body["agent"] == {"id": "ag_1", "slug": "note", "display_name": "Note"}
    assert hook.keys == [call.record.id]
    assert await st.calls.get("u_a", call.record.id) == done
    [entry] = await st.calls.entries("u_a", call.record.id)
    assert (entry.seq, entry.role, entry.text, entry.sealed, entry.error) == (0, "user", "comprar leite", False, None)


async def test_one_shot_short_word_is_kept_at_hang_up():
    stt = Stt("sim")
    call, _ = await make(stt=stt)
    call.on_audio(SPEECH)  # 20 ms, below min_speech_ms
    await call.finish()
    assert call.text == "sim"


async def test_one_shot_ends_by_itself_at_the_turn_limit():
    stt = Stt("long")
    call, _ = await make(stt=stt)
    call.on_audio(frames((SPEECH, 50)))  # 1000 ms = max_turn_ms
    assert call.captured
    call.on_audio(frames((SPEECH, 10)))  # after the limit: ignored
    done = await call.finish()
    assert call.text == "long" and stt.lengths == [44 + 50 * FRAME_BYTES_IN]


async def test_monologue_is_cut_at_pauses_and_joined_in_order():
    stt = Stt("primeira ideia.", "segunda", " ", "terceira")
    call, _ = await make("monologue", stt=stt)
    call.on_audio(frames((SPEECH, 5), (SIL, 6), (SPEECH, 5), (SIL, 6), (SPEECH, 3), (SIL, 6)))
    assert not call.captured
    call.on_audio(frames((SPEECH, 4)))
    done = await call.finish()
    assert call.text == "primeira ideia. segunda terceira"
    assert len(stt.lengths) == 4


async def test_monologue_stops_at_the_call_limit():
    stt = Stt("a", "b")
    call, _ = await make("monologue", stt=stt, max_call_ms=400)
    call.on_audio(frames((SPEECH, 5), (SIL, 6), (SPEECH, 20)))
    assert call.captured
    await call.finish()
    assert call.text == "a b"


async def test_nothing_is_recorded_while_muted_and_mute_ends_nothing():
    stt = Stt("antes depois")
    call, _ = await make(stt=stt)
    call.on_audio(frames((SPEECH, 5)))
    call.on_mute(True)
    call.on_audio(frames((SPEECH, 100)))  # dropped, so the limit is not reached either
    assert not call.captured
    call.on_mute(False)
    call.on_audio(frames((SPEECH, 5)))
    await call.finish()
    assert stt.lengths == [44 + 10 * FRAME_BYTES_IN]


async def test_hang_up_without_speech_is_empty_and_not_delivered():
    hook = Hook()
    call, st = await make(hook=hook)
    call.on_audio(frames((SIL, 30)))
    done = await call.finish()
    assert (done.status, call.text) == ("empty", None) and hook.bodies == []
    assert await st.calls.entries("u_a", call.record.id) == []


async def test_blank_transcript_is_empty():
    call, _ = await make(stt=Stt("  "))
    call.on_audio(SPEECH)
    assert (await call.finish()).status == "empty"


async def test_stt_is_tried_twice():
    stt = Stt(ProviderError("down"), "ok")
    call, _ = await make(stt=stt)
    call.on_audio(SPEECH)
    await call.finish()
    assert call.text == "ok"


async def test_stt_failure_keeps_what_was_heard_and_does_not_deliver():
    stt, hook = Stt("começo", ProviderError("x"), TimeoutError()), Hook()
    call, st = await make("monologue", stt=stt, hook=hook)
    call.on_audio(frames((SPEECH, 5), (SIL, 6), (SPEECH, 5)))
    done = await call.finish()
    assert (done.status, done.error, call.text) == ("failed", "stt_failed", "começo")
    assert hook.bodies == []
    [entry] = await st.calls.entries("u_a", call.record.id)
    assert (entry.text, entry.error) == ("começo", "stt_failed")


async def test_delivery_failure_keeps_the_text_and_the_attempts():
    hook = Hook(500, WebhookError("timeout"), 404)
    call, _ = await make(hook=hook)
    call.on_audio(SPEECH)
    done = await call.finish()
    assert (done.status, done.error, call.text, done.attempts, done.last_http_status) == (
        "failed", "delivery_failed", "olá", 3, 404
    )


async def test_cancelled_processing_is_interrupted_with_its_text():
    class SlowHook(Hook):
        async def send(self, body, **kw):
            await asyncio.sleep(10)

    call, st = await make(hook=SlowHook())
    call.on_audio(SPEECH)
    task = asyncio.create_task(call.finish())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = await st.calls.get("u_a", call.record.id)
    assert (saved.status, saved.error) == ("failed", "interrupted")
    assert [e.text for e in await st.calls.entries("u_a", call.record.id)] == ["olá"]


async def test_background_lets_work_finish_within_the_grace_then_cancels():
    bg = Background(grace_s=0.2)
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(10)

    quick = bg.spawn(asyncio.sleep(0.05, result="done"))
    slow = bg.spawn(forever())
    await started.wait()
    await bg.close()
    assert quick.result() == "done"
    assert slow.cancelled()


async def test_user_deleted_while_processing_ends_quietly():
    class DeletingHook(Hook):
        async def send(self, body, **kw):
            await st.users.delete("u_a")
            return 204

    call, st = await make(hook=DeletingHook())
    call.on_audio(SPEECH)
    done = await call.finish()
    assert done.status == "delivered"
    assert await st.calls.get("u_a", call.record.id) is None
