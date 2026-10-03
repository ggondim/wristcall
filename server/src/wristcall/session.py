"""A call: wires the turn state machine to the providers and the transport (WebSocket)."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from . import protocol
from .audio import FRAME_BYTES_IN, SAMPLE_RATE_IN, FrameAssembler, duration_ms, frame_bytes, pcm16_to_wav
from .config import ProfileConfig
from .providers import ProviderError, ProviderSet
from .sentences import SentenceSplitter
from .turn import State, TurnClosed, TurnMachine
from .vad import Vad

log = logging.getLogger("wristcall.session")


class Transport(Protocol):
    async def send_json(self, msg: dict[str, Any]) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...


async def _with_first_timeout(agen: AsyncIterator[str], first_timeout_s: float) -> AsyncIterator[str]:
    """Passes the stream through, requiring the first chunk to arrive within the deadline."""
    try:
        first = await asyncio.wait_for(anext(agen), first_timeout_s)
    except StopAsyncIteration:
        return
    yield first
    async for item in agen:
        yield item


class CallSession:
    def __init__(
        self,
        profile: ProfileConfig,
        providers: ProviderSet,
        vad: Vad,
        transport: Transport,
        *,
        playback_margin_ms: int = 200,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile
        self.providers = providers
        self.transport = transport
        self.machine = TurnMachine(
            vad,
            silence_ms=profile.vad.silence_ms,
            min_speech_ms=profile.vad.min_speech_ms,
            max_turn_ms=profile.vad.max_turn_ms,
            pre_roll_ms=profile.vad.pre_roll_ms,
        )
        self.history: list[dict[str, str]] = []
        self._frames = FrameAssembler(FRAME_BYTES_IN)
        self._margin_s = playback_margin_ms / 1000
        self._sleep = sleep
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    # ---------- input ----------

    async def on_audio(self, data: bytes) -> None:
        if self.machine.state is not State.LISTENING:
            self._frames.reset()
            return
        for frame in self._frames.push(data):
            closed = self.machine.on_audio(frame)
            if closed:
                await self._start_agent_turn(closed)
                return

    async def on_mute(self, muted: bool) -> None:
        closed = self.machine.on_mute(muted)
        if closed:
            self._frames.reset()
            await self._start_agent_turn(closed)

    async def close(self) -> None:
        self.machine.on_end()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def wait_idle(self) -> None:
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    # ---------- agent turn ----------

    async def _start_agent_turn(self, closed: TurnClosed) -> None:
        await self.transport.send_json(protocol.turn_user_end(closed.reason))
        self._task = asyncio.create_task(self._agent_turn(closed.audio))

    async def _agent_turn(self, audio: bytes) -> None:
        try:
            await self._run_pipeline(audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unexpected failure in the agent turn")
            await self._safe_send(protocol.error(protocol.ErrorCode.INTERNAL, "Internal error.", False))
        finally:
            self.machine.on_agent_done()

    async def _safe_send(self, msg: dict[str, Any]) -> None:
        try:
            await self.transport.send_json(msg)
        except Exception:
            log.debug("transport closed while sending %s", msg.get("type"))

    async def _run_pipeline(self, audio: bytes) -> None:
        timeouts = self.profile.timeouts
        try:
            text = await asyncio.wait_for(
                self.providers.stt.transcribe(pcm16_to_wav(audio, SAMPLE_RATE_IN), self.profile.language),
                timeouts.stt_s,
            )
        except (ProviderError, TimeoutError) as e:
            log.warning("STT failed: %s", e)
            # Error first, then back to listening: frames accepted while sending would be wiped by on_agent_done.
            await self.transport.send_json(protocol.error(protocol.ErrorCode.STT_FAILED, "I could not understand the audio.", False))
            self.machine.on_transcript("")
            return
        self.machine.on_transcript(text)
        if not text.strip():
            return
        await self.transport.send_json(protocol.transcript("user", text))
        self.history.append({"role": "user", "content": text})
        messages = ([{"role": "system", "content": self.profile.system_prompt}] if self.profile.system_prompt else []) + self.history

        speaker = _Speaker(self)
        try:
            full_text = await self._respond(messages, speaker)
            if full_text:
                self.history.append({"role": "assistant", "content": full_text})
                await self.transport.send_json(protocol.transcript("assistant", full_text))
        except Exception:
            # Unexpected failure after the audio may have started: close the turn on the watch
            # and wait for playback before propagating (_agent_turn sends error{internal}).
            # CancelledError is not an Exception: close() stays immediate.
            await speaker.abort()
            raise
        await speaker.finish()

    async def _respond(self, messages: list[dict[str, str]], speaker: "_Speaker") -> str:
        """Generates and speaks the response; returns the text to record (empty if nothing was said)."""
        spoken: list[str] = []
        splitter = SentenceSplitter()
        responder_error: Exception | None = None
        try:
            stream = _with_first_timeout(self.providers.responder.respond(messages), self.profile.timeouts.first_token_s)
            async for piece in stream:
                spoken.append(piece)
                for sentence in splitter.push(piece):
                    await speaker.say(sentence)
        except (ProviderError, TimeoutError) as e:
            responder_error = e
            log.warning("responder failed: %s", e)
        if responder_error is None:
            for sentence in splitter.flush():
                await speaker.say(sentence)
        full_text = "".join(spoken).strip()
        if not full_text:
            # Failed before the first text or ended without text (empty stream or only whitespace):
            # the user must not be left in silence.
            await self.transport.send_json(
                protocol.error(protocol.ErrorCode.RESPONDER_FAILED, "I could not generate a response.", False)
            )
            await speaker.say(self.profile.fallback_message)
        elif responder_error is not None:
            full_text = " ".join(speaker.sentences_said).strip()
            await self.transport.send_json(
                protocol.error(protocol.ErrorCode.RESPONDER_FAILED, "The response was interrupted.", False)
            )
        await speaker.flush_audio()
        return full_text


class _Speaker:
    """Sends the TTS audio in 20 ms frames and waits for playback to finish on the watch."""

    def __init__(self, session: CallSession) -> None:
        self._s = session
        rate = session.providers.tts.sample_rate
        self._rate = rate
        self._frames = FrameAssembler(frame_bytes(rate))
        self._started = False
        self._tts_failed = False
        # Moment (on the server clock) when the watch should finish playing what has already been sent.
        self._play_end = 0.0
        self.sentences_said: list[str] = []

    async def _send(self, frame: bytes) -> None:
        if not self._started:
            self._started = True
            self._s.machine.on_agent_start()
            await self._s.transport.send_json(protocol.agent_start())
        await self._s.transport.send_bytes(frame)
        # If the frame arrives after the watch has already played everything (gap between sentences), it plays starting now.
        now = self._s._clock()
        self._play_end = max(self._play_end, now) + duration_ms(frame, self._rate) / 1000

    async def say(self, sentence: str) -> None:
        if self._tts_failed:
            return
        try:
            async with asyncio.timeout(self._s.profile.timeouts.tts_s):
                async for chunk in self._s.providers.tts.synthesize(sentence):
                    for frame in self._frames.push(chunk):
                        await self._send(frame)
        except (ProviderError, TimeoutError) as e:
            log.warning("TTS failed: %s", e)
            self._tts_failed = True
            await self._s.transport.send_json(protocol.error(protocol.ErrorCode.TTS_FAILED, "Failed to generate the voice.", False))
            return
        self.sentences_said.append(sentence)

    async def flush_audio(self) -> None:
        """Sends the rest of the audio, padded with silence up to a full frame."""
        tail = self._frames.drain()
        if tail:
            await self._send(tail)

    async def abort(self) -> None:
        """Closes the turn on the watch after an unexpected failure, without masking the original failure."""
        try:
            await self.flush_audio()
            await self.finish()
        except Exception:
            log.debug("could not close the agent turn after failure", exc_info=True)

    async def finish(self) -> None:
        """After the last frame: turn.agent_end, then wait for the watch to finish playing."""
        if not self._started:
            return
        await self._s.transport.send_json(protocol.agent_end())
        remaining_s = max(0.0, self._play_end - self._s._clock()) + self._s._margin_s
        await self._s._sleep(remaining_s)
