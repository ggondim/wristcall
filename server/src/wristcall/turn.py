"""State machine for a call turn. Pure: no I/O, no clock, no asyncio."""

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from .audio import FRAME_MS
from .vad import Vad


class State(StrEnum):
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ENDED = "ended"


@dataclass(frozen=True)
class TurnClosed:
    audio: bytes
    reason: Literal["vad", "mute", "limit"]


class InvalidTransition(Exception):
    pass


class TurnMachine:
    def __init__(
        self,
        vad: Vad,
        *,
        silence_ms: int = 800,
        min_speech_ms: int = 300,
        max_turn_ms: int = 60_000,
        pre_roll_ms: int = 300,
        frame_ms: int = FRAME_MS,
    ) -> None:
        self._vad = vad
        self._silence_limit = silence_ms
        self._min_speech = min_speech_ms
        self._max_turn = max_turn_ms
        self._frame_ms = frame_ms
        self._pre_roll: deque[bytes] = deque(maxlen=max(1, pre_roll_ms // frame_ms))
        self.state = State.LISTENING
        self.muted = False
        self._buffer = bytearray()
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._turn_ms = 0

    def _reset_turn(self) -> None:
        self._pre_roll.clear()
        self._buffer = bytearray()
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._turn_ms = 0
        self._vad.reset()

    def _close(self, reason: Literal["vad", "mute", "limit"]) -> TurnClosed:
        audio = bytes(self._buffer)
        self.state = State.TRANSCRIBING
        self._reset_turn()
        return TurnClosed(audio=audio, reason=reason)

    def _expect(self, state: State) -> None:
        if self.state is not state:
            raise InvalidTransition(f"expected {state}, current state {self.state}")

    def on_audio(self, frame: bytes) -> TurnClosed | None:
        if self.state is not State.LISTENING or self.muted:
            return None
        speech = self._vad.is_speech(frame)
        if not self._in_speech:
            if not speech:
                self._pre_roll.append(frame)
                return None
            self._in_speech = True
            for f in self._pre_roll:
                self._buffer.extend(f)
            self._pre_roll.clear()
        self._buffer.extend(frame)
        self._turn_ms += self._frame_ms
        if speech:
            self._speech_ms += self._frame_ms
            self._silence_ms = 0
        else:
            self._silence_ms += self._frame_ms
        if self._turn_ms >= self._max_turn:
            return self._close("limit")
        if self._silence_ms >= self._silence_limit:
            if self._speech_ms >= self._min_speech:
                return self._close("vad")
            self._reset_turn()
        return None

    def on_mute(self, muted: bool) -> TurnClosed | None:
        was = self.muted
        self.muted = muted
        if self.state is not State.LISTENING or muted == was:
            return None
        if muted and self._in_speech:
            return self._close("mute")
        self._reset_turn()
        return None

    def on_transcript(self, text: str) -> None:
        self._expect(State.TRANSCRIBING)
        self.state = State.THINKING if text.strip() else State.LISTENING

    def on_agent_start(self) -> None:
        self._expect(State.THINKING)
        self.state = State.SPEAKING

    def on_agent_done(self) -> None:
        if self.state is State.ENDED:
            return
        self.state = State.LISTENING
        self._reset_turn()

    def on_end(self) -> None:
        self.state = State.ENDED
