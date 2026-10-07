import pytest

from wristcall.turn import InvalidTransition, State, TurnClosed, TurnMachine

SPEECH = b"\x01" * 640
SIL = b"\x00" * 640


class ByteVad:
    """Speech when the first byte of the frame is non zero."""

    def __init__(self) -> None:
        self.resets = 0

    def is_speech(self, frame: bytes) -> bool:
        return frame[0] != 0

    def reset(self) -> None:
        self.resets += 1


def machine(vad=None, **kw) -> TurnMachine:
    params = {"silence_ms": 800, "min_speech_ms": 300, "max_turn_ms": 60_000, "pre_roll_ms": 300, **kw}
    return TurnMachine(vad or ByteVad(), **params)


def feed(m: TurnMachine, frame: bytes, n: int) -> list[TurnClosed]:
    return [t for t in (m.on_audio(frame) for _ in range(n)) if t is not None]


def test_silence_never_closes():
    m = machine()
    assert feed(m, SIL, 200) == []
    assert m.state is State.LISTENING


def test_vad_closes_turn_after_silence_with_pre_roll():
    m = machine()
    feed(m, SIL, 5)
    assert feed(m, SPEECH, 25) == []
    closed = feed(m, SIL, 40)
    assert len(closed) == 1 and closed[0].reason == "vad"
    assert len(closed[0].audio) == (5 + 25 + 40) * 640
    assert m.state is State.TRANSCRIBING


def test_pre_roll_is_bounded():
    m = machine()
    feed(m, SIL, 100)
    feed(m, SPEECH, 25)
    assert len(feed(m, SIL, 40)[0].audio) == (15 + 25 + 40) * 640


def test_short_blip_is_discarded():
    m = machine()
    feed(m, SPEECH, 10)
    assert feed(m, SIL, 40) == []
    assert m.state is State.LISTENING
    feed(m, SPEECH, 25)
    assert len(feed(m, SIL, 40)[0].audio) == (25 + 40) * 640


def test_mute_closes_turn_immediately():
    m = machine()
    feed(m, SPEECH, 25)
    assert m.on_mute(True) == TurnClosed(audio=SPEECH * 25, reason="mute")
    assert m.state is State.TRANSCRIBING and m.muted


def test_mute_closes_turn_with_short_speech():
    """Spec 4.3: mute:true with a non empty buffer closes the turn, even below min_speech_ms."""
    m = machine()
    feed(m, SIL, 2)
    feed(m, SPEECH, 5)
    assert m.on_mute(True) == TurnClosed(audio=SIL * 2 + SPEECH * 5, reason="mute")
    assert m.state is State.TRANSCRIBING


def test_mute_without_speech_does_nothing_and_frames_are_ignored_while_muted():
    m = machine()
    assert m.on_mute(True) is None
    assert feed(m, SPEECH, 50) == [] and feed(m, SIL, 50) == []
    assert m.state is State.LISTENING


def test_unmute_starts_fresh():
    vad = ByteVad()
    m = machine(vad)
    m.on_mute(True)
    before = vad.resets
    m.on_mute(False)
    assert vad.resets == before + 1 and not m.muted
    feed(m, SPEECH, 25)
    assert feed(m, SIL, 40)[0].reason == "vad"


def test_agent_cycle_discards_audio_until_done():
    m = machine()
    feed(m, SPEECH, 25)
    feed(m, SIL, 40)
    assert feed(m, SPEECH, 100) == []
    m.on_transcript("hi")
    assert m.state is State.THINKING
    m.on_agent_start()
    assert m.state is State.SPEAKING
    assert feed(m, SPEECH, 100) == []
    m.on_agent_done()
    assert m.state is State.LISTENING
    feed(m, SPEECH, 25)
    assert feed(m, SIL, 40)[0].reason == "vad"


def test_empty_transcript_returns_to_listening():
    m = machine()
    feed(m, SPEECH, 25)
    feed(m, SIL, 40)
    m.on_transcript("   ")
    assert m.state is State.LISTENING


def test_max_turn_limit():
    m = machine(max_turn_ms=1000)
    closed = feed(m, SPEECH, 60)
    assert [t.reason for t in closed] == ["limit"]
    assert len(closed[0].audio) == 50 * 640


def test_mute_persists_through_agent_turn():
    m = machine()
    feed(m, SPEECH, 25)
    m.on_mute(True)
    m.on_transcript("hi")
    m.on_agent_start()
    m.on_agent_done()
    assert m.state is State.LISTENING and m.muted
    assert feed(m, SPEECH, 25) == [] and feed(m, SIL, 40) == []
    m.on_mute(False)
    feed(m, SPEECH, 25)
    assert feed(m, SIL, 40)[0].reason == "vad"


def test_mute_during_agent_turn_is_recorded_but_does_not_close():
    m = machine()
    feed(m, SPEECH, 25)
    feed(m, SIL, 40)
    m.on_transcript("hi")
    assert m.on_mute(True) is None
    assert m.muted and m.state is State.THINKING


def test_invalid_transitions():
    m = machine()
    with pytest.raises(InvalidTransition):
        m.on_transcript("hi")
    with pytest.raises(InvalidTransition):
        m.on_agent_start()


def test_end_is_terminal():
    m = machine()
    m.on_end()
    assert feed(m, SPEECH, 25) == []
    m.on_agent_done()
    assert m.state is State.ENDED


# ---------- manual turn end ----------


def test_manual_silence_never_closes_after_speech():
    m = machine(turn_end="manual")
    feed(m, SPEECH, 25)
    assert feed(m, SIL, 1000) == []
    assert m.state is State.LISTENING


def test_manual_mute_closes_with_all_audio_including_pauses_and_pre_roll():
    m = machine(turn_end="manual")
    feed(m, SIL, 5)
    feed(m, SPEECH, 25)
    feed(m, SIL, 200)
    feed(m, SPEECH, 10)
    feed(m, SIL, 3)
    closed = m.on_mute(True)
    assert closed == TurnClosed(audio=SIL * 5 + SPEECH * 25 + SIL * 200 + SPEECH * 10 + SIL * 3, reason="mute")
    assert m.state is State.TRANSCRIBING


def test_manual_short_speech_followed_by_long_silence_is_kept():
    """A short answer ("yes"), a pause and then mute: nothing is dropped, mute closes even below min_speech_ms."""
    m = machine(turn_end="manual")
    feed(m, SPEECH, 2)
    assert feed(m, SIL, 100) == []
    assert m.on_mute(True) == TurnClosed(audio=SPEECH * 2 + SIL * 100, reason="mute")


def test_manual_pre_roll_is_bounded():
    m = machine(turn_end="manual")
    feed(m, SIL, 100)
    feed(m, SPEECH, 25)
    assert len(m.on_mute(True).audio) == (15 + 25) * 640


def test_manual_mute_without_speech_does_nothing():
    m = machine(turn_end="manual")
    feed(m, SIL, 100)
    assert m.on_mute(True) is None
    assert feed(m, SPEECH, 50) == []
    assert m.state is State.LISTENING


def test_manual_max_turn_limit_still_applies_from_speech_start():
    m = machine(turn_end="manual", max_turn_ms=1000)
    feed(m, SIL, 100)
    feed(m, SPEECH, 10)
    closed = feed(m, SIL, 100)
    assert [t.reason for t in closed] == ["limit"]
    assert len(closed[0].audio) == (15 + 50) * 640


def test_manual_next_turn_is_manual_too():
    m = machine(turn_end="manual")
    feed(m, SPEECH, 25)
    m.on_mute(True)
    m.on_transcript("hi")
    m.on_agent_start()
    m.on_agent_done()
    m.on_mute(False)
    feed(m, SPEECH, 25)
    assert feed(m, SIL, 200) == []
    assert m.on_mute(True).reason == "mute"


def test_auto_is_the_default():
    m = machine()
    assert m.turn_end == "auto"
    assert machine(turn_end="manual").turn_end == "manual"
