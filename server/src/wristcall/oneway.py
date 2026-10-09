"""One-way calls (one-shot, monologue): record, transcribe, deliver the text to the agent's webhook. No answer, no voice.

While the call is open, speech is cut into segments (a monologue at pauses or at the turn limit; a one-shot is
a single segment) and each is transcribed in the background. At hang-up the last segment is kept, however
short, the texts are joined in order and delivered (delivery.py). The call's history record (history.CallLog) tells
the client how it went and keeps the transcript.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from .agents import Agent, OneWayProviders
from .audio import FRAME_BYTES_IN, FRAME_MS, SAMPLE_RATE_IN, FrameAssembler, pcm16_to_wav
from .delivery import DeliveryPolicy, deliver, payload
from .history import CallLog, new_call_id
from .providers import ProviderError
from .storage import CallRecord
from .turn import TurnMachine
from .vad import Vad

log = logging.getLogger("wristcall.oneway")

STT_TRIES = 2

__all__ = ["Background", "OneWayCall", "new_call_id"]


class OneWayCall:
    def __init__(
        self,
        agent: Agent,
        providers: OneWayProviders,
        vad: Vad,
        call_log: CallLog,
        *,
        max_call_ms: int,
        policy: DeliveryPolicy = DeliveryPolicy(),
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.agent = agent
        self._log = call_log
        self._providers = providers
        self._policy = policy
        self._now = now
        self._sleep = sleep
        vc = agent.spec.vad
        # A one-shot is one segment that only the hang-up or the limit ends ("manual": silence never does).
        # A monologue is cut at pauses ("auto") so that long talks reach the STT in pieces.
        self._machine = TurnMachine(
            vad,
            silence_ms=vc.silence_ms,
            min_speech_ms=vc.min_speech_ms,
            max_turn_ms=vc.max_turn_ms,
            pre_roll_ms=vc.pre_roll_ms,
            turn_end="manual" if agent.call_type == "one-shot" else "auto",
        )
        self._frames = FrameAssembler(FRAME_BYTES_IN)
        self._max_call_ms = max_call_ms
        self._heard_ms = 0
        self._muted = False
        self._captured = False
        self._stt_lock = asyncio.Lock()
        self._segments: list[asyncio.Task[str | None]] = []
        self.text: str | None = None  # the transcript, once known (also kept in the history)

    @property
    def record(self) -> CallRecord:
        return self._log.record

    @property
    def captured(self) -> bool:
        """The server stopped recording (limit reached); the client should be told and the call closed."""
        return self._captured

    # ---------- capture (while the WebSocket is open) ----------

    def on_audio(self, data: bytes) -> None:
        # Mute does not end anything here, but nothing is recorded while muted.
        if self._captured or self._muted:
            return
        for frame in self._frames.push(data):
            self._heard_ms += FRAME_MS
            closed = self._machine.on_audio(frame)
            if closed:
                self._segment(closed.audio)
                if self.agent.call_type == "one-shot":
                    self._captured = True
                    return
                self._machine.on_agent_done()  # back to listening for the next segment
            if self._heard_ms >= self._max_call_ms:
                self._stop()
                return

    def on_mute(self, muted: bool) -> None:
        self._muted = muted

    def _stop(self) -> None:
        closed = self._machine.flush()
        if closed:
            self._segment(closed.audio)
        self._captured = True

    def _segment(self, audio: bytes) -> None:
        self._segments.append(asyncio.create_task(self._transcribe(audio)))

    async def _transcribe(self, audio: bytes) -> str | None:
        """The segment's text; None if the STT failed every try. One at a time, in order."""
        wav = pcm16_to_wav(audio, SAMPLE_RATE_IN)
        async with self._stt_lock:
            for attempt in range(1, STT_TRIES + 1):
                try:
                    return await asyncio.wait_for(
                        self._providers.stt.transcribe(wav, self.agent.spec.language), self.agent.spec.timeouts.stt_s
                    )
                except (ProviderError, TimeoutError) as e:
                    log.warning("call %s: STT attempt %d failed: %s", self.record.id, attempt, e)
        return None

    # ---------- after hang-up (background) ----------

    async def finish(self) -> CallRecord:
        """Hang-up (or connection lost): keeps the last segment, then transcribes and delivers. Never raises
        but for cancellation, which leaves the call failed as "interrupted"."""
        if not self._captured:
            self._stop()
        ended = self._now()
        await self._log.save(status="processing", ended_at=ended)
        try:
            texts = await asyncio.gather(*self._segments)
            text = " ".join(t.strip() for t in texts if t and t.strip())
            if any(t is None for t in texts):
                if text:
                    self.text = text
                    await self._log.add("user", text, "stt_failed")
                return await self._log.save(status="failed", error="stt_failed", finished=True)
            if not text:
                return await self._log.save(status="empty", finished=True)
            # Recorded before delivering: a server stopped mid-delivery keeps the text.
            self.text = text
            await self._log.add("user", text)
            body = payload(
                call_id=self.record.id,
                call_type=self.agent.call_type,
                agent={"id": self.agent.id, "slug": self.agent.slug, "display_name": self.agent.display_name},
                language=self.agent.spec.language,
                text=text,
                started_at=self.record.created_at,
                ended_at=ended,
            )
            result = await deliver(
                self._providers.webhook, body, idempotency_key=self.record.id, policy=self._policy, sleep=self._sleep
            )
            return await self._log.save(
                status="delivered" if result.delivered else "failed",
                error=None if result.delivered else "delivery_failed",
                attempts=result.attempts,
                last_http_status=result.last_http_status,
                finished=True,
            )
        except asyncio.CancelledError:
            for task in self._segments:
                task.cancel()
            await self._log.save(status="failed", error="interrupted", finished=True)
            raise
        except Exception:
            log.exception("call %s: unexpected failure after hang-up", self.record.id)
            return await self._log.save(status="failed", error="internal", finished=True)


class Background:
    """Work that outlives the WebSocket (one-way calls after hang-up). When the server stops, it gets a few
    seconds to finish (below the 10 s Docker waits before killing), then is cancelled."""

    def __init__(self, grace_s: float = 8.0) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._grace_s = grace_s

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def close(self) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=self._grace_s)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
