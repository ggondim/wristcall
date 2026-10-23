"""The node's counters sent in the heartbeat (cloud mode, server 0.11.0): buckets, the interval kept until the Cloud
accepts a beat, provider names, peak, outbox drops, the providers' times and the first audio of an answer."""

import time

import pytest
from cloud_app_fixtures import cloud_app, cloud_config, wait_ready
from cloud_app_fixtures import new_fake as new_app_fake
from cloud_node_helpers import client_of, new_fake
from conftest import fake_config
from fastapi.testclient import TestClient
from test_oneway import SPEECH
from test_oneway import Stt as OneWayStt
from test_oneway import make as make_one_way
from test_session import ByteVad, FakeTransport, Llm, Sleeps, Stt, Tts, speak

from wristcall.admission import CallSlots
from wristcall.audio import wav_to_pcm16
from wristcall.cloud_node.heartbeat import Heartbeat
from wristcall.cloud_node.outbox import CallRegistry, Outbox, OutboxItem
from wristcall.cloud_node.stats import (
    BUCKETS_MS,
    NodeStats,
    TimedSpeechToText,
    TimedTextToSpeech,
    bucket,
    note_failure,
)
from wristcall.config import ConcurrentCallsConfig
from wristcall.providers import ProviderError, ProviderSet
from wristcall.session import CallSession


@pytest.fixture
def fake():
    return new_fake()


@pytest.fixture
def client(fake):
    return client_of(fake)


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --- NodeStats ------------------------------------------------------------------------------------------------------


def test_snapshot_buckets_first_audio():
    stats = NodeStats()
    stats.first_audio(480)
    stats.first_audio(2600)
    stats.first_audio(20000)
    assert stats.snapshot()["first_audio_ms"] == {"500": 1, "3000": 1, "inf": 1}


def test_bucket_bounds():
    assert [bucket(b) for b in BUCKETS_MS] == [str(b) for b in BUCKETS_MS]  # a bound is in its own bucket
    assert (bucket(0), bucket(250.1), bucket(15000.1)) == ("250", "500", "inf")


def test_empty_snapshot_has_the_ten_keys():
    assert NodeStats().snapshot() == {
        "calls": {"conversation": 0, "one_way": 0}, "busy": {"conversation": 0, "one_way": 0}, "not_ready": 0,
        "peak": {"conversation": 0, "one_way": 0}, "first_audio_ms": {}, "stt_ms": {}, "tts_ms": {},
        "provider_failures": {}, "cloud_unreachable_s": 0, "outbox_dropped": 0,
    }


def test_commit_keeps_what_arrived_after_snapshot():
    stats = NodeStats()
    stats.busy("one_way")
    s = stats.snapshot()
    stats.busy("one_way")
    stats.commit(s)
    assert stats.snapshot()["busy"]["one_way"] == 1


def test_commit_takes_off_every_counter():
    stats = NodeStats()
    stats.call_admitted("conversation")
    stats.not_ready()
    stats.first_audio(900)
    stats.provider_time("stt", "stt-default", 300)
    stats.provider_time("tts", "user", 700)
    stats.provider_failed("stt-default")
    stats.cloud_unreachable(2.4)
    stats.outbox_dropped(3)
    sent = stats.snapshot()
    assert sent["cloud_unreachable_s"] == 3  # rounded up
    stats.provider_time("stt", "stt-default", 300)  # after the snapshot: stays
    stats.commit(sent)
    after = stats.snapshot()
    assert after["calls"] == {"conversation": 0, "one_way": 0}
    assert (after["not_ready"], after["first_audio_ms"], after["tts_ms"]) == (0, {}, {})
    assert after["stt_ms"] == {"stt-default": {"500": 1}}
    assert (after["provider_failures"], after["cloud_unreachable_s"], after["outbox_dropped"]) == ({}, 0, 0)


def test_unknown_kind_is_ignored():
    stats = NodeStats()
    stats.busy("monologue")
    stats.call_admitted("x")
    assert stats.snapshot()["busy"] == {"conversation": 0, "one_way": 0}


def test_provider_names_capped_and_user_bucket():
    stats = NodeStats()
    assert stats.key_for(None) == "user"  # the user's own endpoint
    names = [f"stt-{n}" for n in range(9)]
    keys = [stats.key_for(n) for n in names]
    assert keys == names[:6] + ["other"] * 3  # the 7th operator name on goes to `other`
    assert stats.key_for("stt-0") == "stt-0"  # a name already counted keeps its key
    # Names that are not safe to send, or would read as the reserved ones, go to `other`.
    for odd in ("Stt", "a.b", "$x", "-x", "", "x" * 33, "user", "other"):
        assert stats.key_for(odd) == "other", odd
    for name in names + ["Bad.Name"]:
        stats.provider_time("stt", name, 100)
        stats.provider_failed(name)
    stats.provider_time("tts", "user", 100)
    snap = stats.snapshot()
    assert set(snap["stt_ms"]) == set(names[:6]) | {"other"}
    assert snap["stt_ms"]["other"] == {"250": 4}
    assert len(snap["provider_failures"]) <= 8 and snap["provider_failures"]["other"] == 4
    assert snap["tts_ms"] == {"user": {"250": 1}}


def test_peak_tracks_max_and_resets_to_current():
    stats = NodeStats()
    stats.active({"conversation": 1, "one_way": 0})
    stats.active({"conversation": 2, "one_way": 1})
    stats.active({"conversation": 1, "one_way": 0})
    sent = stats.snapshot()
    assert sent["peak"] == {"conversation": 2, "one_way": 1}
    stats.commit(sent)
    assert stats.snapshot()["peak"] == {"conversation": 1, "one_way": 0}  # the calls open now


# --- the heartbeat --------------------------------------------------------------------------------------------------


def heartbeat(client, clock, slots=None) -> Heartbeat:
    return Heartbeat(
        client, CallRegistry(), slots or CallSlots(ConcurrentCallsConfig()), ConcurrentCallsConfig(conversation=3),
        version="0.11.0", clock=clock, stats=NodeStats(clock=clock),
    )


async def test_unaccepted_beats_accumulate(fake, client):
    clock = Clock()
    hb = heartbeat(client, clock)
    fake.heartbeat_answers = [503, 503]
    hb.stats.busy("conversation")
    assert await hb.beat() is False
    clock.now += 15
    hb.stats.busy("conversation")
    assert await hb.beat() is False
    clock.now += 15
    assert await hb.beat() is True
    third = fake.heartbeat_bodies[2]["stats"]
    assert third["busy"]["conversation"] == 2  # both refusals, sent once
    assert third["cloud_unreachable_s"] == 30  # from the first refused beat to the accepted one
    assert third["cloud_unreachable_s"] >= 1
    clock.now += 15
    assert await hb.beat() is True
    fourth = fake.heartbeat_bodies[3]["stats"]
    assert (fourth["busy"]["conversation"], fourth["cloud_unreachable_s"]) == (0, 0)
    assert fake.node_stats == [third, fourth]


async def test_cloud_down_counts_as_unreachable(fake, client):
    clock = Clock()
    hb = heartbeat(client, clock)
    fake.down = True
    assert await hb.beat() is False
    clock.now += 7.5
    fake.down = False
    assert await hb.beat() is True
    assert fake.node_stats[-1]["cloud_unreachable_s"] == 8


async def test_peak_in_the_heartbeat(fake, client):
    slots = CallSlots(ConcurrentCallsConfig(conversation=3))
    hb = heartbeat(client, Clock(), slots)
    held = [slots.try_acquire("conversation") for _ in range(2)]
    hb.stats.active(slots.active())
    held[0].release()
    assert await hb.beat() is True
    assert fake.node_stats[-1]["peak"]["conversation"] == 2
    assert await hb.beat() is True
    assert fake.node_stats[-1]["peak"]["conversation"] == 1  # the call still open


# --- the outbox -----------------------------------------------------------------------------------------------------


async def test_outbox_drop_counts(fake, client):
    stats = NodeStats()
    registry = CallRegistry()
    outbox = Outbox(client, registry, max_items=2, stats=stats)
    for seq in range(5):
        outbox.put(OutboxItem("c_1", "entries", "/v1/node/calls/c_1/entries", {"entries": [{"seq": seq}]}, "g"))
    assert outbox.dropped == 3
    assert stats.snapshot()["outbox_dropped"] == 3


# --- the voice providers' times -------------------------------------------------------------------------------------


class StepStt:
    def __init__(self, clock: Clock, seconds: float, exc: Exception | None = None) -> None:
        self.clock, self.seconds, self.exc = clock, seconds, exc

    async def transcribe(self, wav, language):
        self.clock.now += self.seconds
        if self.exc:
            raise self.exc
        return "hi"


class StepTts:
    sample_rate = 24000
    voice = "alloy"

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def synthesize(self, text):
        self.clock.now += 0.3
        yield b"a"
        self.clock.now += 5
        yield b"b"


async def test_timed_providers():
    clock = Clock()
    stats = NodeStats(clock=clock)
    stt = TimedSpeechToText(StepStt(clock, 0.9), stats, stats.key_for("stt-default"))
    tts = TimedTextToSpeech(StepTts(clock), stats, stats.key_for(None))
    assert await stt.transcribe(b"", "en") == "hi"
    assert [c async for c in tts.synthesize("x")] == [b"a", b"b"]
    assert (tts.sample_rate, tts.voice) == (24000, "alloy")  # read through to the provider
    failing = TimedSpeechToText(StepStt(clock, 0.1, ProviderError("down")), stats, "stt-default")
    with pytest.raises(ProviderError):
        await failing.transcribe(b"", "en")
    note_failure(failing)
    note_failure(StepStt(clock, 0))  # not counted (self-hosted): nothing happens
    snap = stats.snapshot()
    assert snap["stt_ms"] == {"stt-default": {"1000": 1}}  # a failure has no time
    assert snap["tts_ms"] == {"user": {"500": 1}}  # to the first chunk
    assert snap["provider_failures"] == {"stt-default": 1}


def session(stats: NodeStats, *, stt=None, tts=None, seen=None, **profile_over) -> tuple[CallSession, FakeTransport]:
    profile = fake_config(**profile_over).profiles["default"]
    transport = FakeTransport()
    providers = ProviderSet(
        TimedSpeechToText(stt or Stt(), stats, "stt"), Llm(), TimedTextToSpeech(tts or Tts(), stats, "tts")
    )
    s = CallSession(
        profile, providers, ByteVad(), transport, sleep=Sleeps(), on_first_audio=None if seen is None else seen.append
    )
    return s, transport


async def test_first_audio_once_per_answer():
    stats = NodeStats()
    seen: list[float] = []
    s, t = session(stats, seen=seen)
    started = time.monotonic()
    await speak(s)
    await s.wait_idle()
    await speak(s)
    await s.wait_idle()
    assert len(seen) == 2 and all(0 <= ms <= (time.monotonic() - started) * 1000 for ms in seen)
    snap = stats.snapshot()
    assert sum(snap["stt_ms"]["stt"].values()) == 2 and sum(snap["tts_ms"]["tts"].values()) >= 2


@pytest.mark.parametrize("case", ["stt_error", "stt_timeout", "tts_error"])
async def test_session_failures_count(case):
    stats = NodeStats()
    if case == "stt_error":
        s, _ = session(stats, stt=Stt(exc=ProviderError("500")))
    elif case == "stt_timeout":
        s, _ = session(stats, stt=Stt(delay=0.5), timeouts={"stt_s": 0.05})
    else:
        s, _ = session(stats, tts=Tts(fail=True))
    await speak(s)
    await s.wait_idle()
    assert stats.snapshot()["provider_failures"] == {"tts" if case == "tts_error" else "stt": 1}


async def test_one_way_stt_failures_count():
    stats = NodeStats()
    stt = TimedSpeechToText(OneWayStt(ProviderError("down"), TimeoutError(), "ok"), stats, "stt-default")
    call, _ = await make_one_way(stt=stt)
    call.on_audio(SPEECH)
    await call.finish()
    snap = stats.snapshot()
    assert snap["provider_failures"] == {"stt-default": 2}  # each failed try
    assert snap["first_audio_ms"] == {}  # a one-way call has no answer to time


# --- the node -------------------------------------------------------------------------------------------------------

VOICE_NOTES = {
    "id": "ag_0123456789ab", "slug": "voice-notes", "display_name": "Voice notes", "icon": "mic", "color": None,
    "call_type": "one-shot", "position": 0, "created_at": 1.0, "updated_at": 1.0, "account": "acc-1",
    "spec": {"language": "en", "stt": {"provider": "stt-default"}, "action": None, "retention_days": None},
}
START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}


def test_node_counts_a_call_the_cloud_opened(fixtures_dir):
    fake = new_app_fake()
    fake.plans["acc-1"] = {"plan": "full", "source": "beta", "expires_at": None}
    fake.agents[VOICE_NOTES["id"]] = dict(VOICE_NOTES)
    config = cloud_config(providers={"stt-default": {"type": "fake_stt", "text": "buy oat milk"}})
    token = {"Authorization": f"Bearer {fake.user_token('acc-1', 'dev-watch', 'watchos')}"}
    with TestClient(cloud_app(fake, config)) as c:
        wait_ready(c)
        with c.websocket_connect("/v1/call", headers=token) as ws:
            ws.send_json(START)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready", ready
            pcm, _rate = wav_to_pcm16((fixtures_dir / "speech_pt_16k.wav").read_bytes())
            ws.send_bytes(pcm[: 16000 * 2 * 2])
            ws.send_json({"type": "session.end"})
        deadline = time.monotonic() + 5
        while fake.calls[ready["call_id"]]["status"] != "delivered" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fake.calls[ready["call_id"]]["status"] == "delivered"
        snaps = [*fake.node_stats, c.app.state.cloud_node.stats.snapshot()]
    assert sum(s["calls"]["one_way"] for s in snaps) == 1
    assert max(s["peak"]["one_way"] for s in snaps) == 1
    assert sum(sum(s["stt_ms"].get("stt-default", {}).values()) for s in snaps) >= 1
    assert all(set(s["stt_ms"]) <= {"stt-default"} for s in snaps)
