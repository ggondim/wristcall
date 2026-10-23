"""The node's counters (cloud mode), sent in the heartbeat as `stats`: calls taken and refused, the most calls open at
once, the time to the first audio of an answer, the voice providers' times and failures, how long the Cloud could not
be reached and the outbox items dropped. Like a shop's till roll: every heartbeat the Cloud accepts takes the totals
since the last one, and what happened while the head office could not be reached waits on the roll for the next.

Counting must never take the node down: provider names are cut to a safe set (`user`, `other` and at most six of the
operator's), and nothing here raises on a number.
"""

import math
import re
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

# The upper bounds (ms) of the histograms' buckets; a time above the last one goes to "inf".
BUCKETS_MS = (250, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10000, 15000)
KINDS = ("conversation", "one_way")
USER = "user"  # the user's own endpoint
OTHER = "other"  # a name that is not safe to send, or one past the cap
MAX_OPERATOR_NAMES = 6  # with `user` and `other`, at most 8 keys per map
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

Stage = Literal["stt", "tts"]


def bucket(ms: float) -> str:
    """The histogram key of a time: the smallest bound it does not pass, or "inf"."""
    for bound in BUCKETS_MS:
        if ms <= bound:
            return str(bound)
    return "inf"


def _sub(counts: dict[str, int], sent: Any) -> None:
    """Takes what was sent off `counts` (never below 0) and forgets the keys left at 0."""
    if not isinstance(sent, dict):
        return
    for key, n in sent.items():
        if key in counts and isinstance(n, int):
            counts[key] = max(0, counts[key] - n)
            if counts[key] == 0:
                del counts[key]


class NodeStats:
    """The counters of the interval since the last heartbeat the Cloud accepted."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._calls = dict.fromkeys(KINDS, 0)
        self._busy = dict.fromkeys(KINDS, 0)
        self._not_ready = 0
        self._peak = dict.fromkeys(KINDS, 0)
        self._current = dict.fromkeys(KINDS, 0)  # calls open at the last `active`
        self._first_audio: dict[str, int] = {}
        self._times: dict[Stage, dict[str, dict[str, int]]] = {"stt": {}, "tts": {}}
        self._failures: dict[str, int] = {}
        self._unreachable_s = 0.0
        self._dropped = 0
        self._operator_names: list[str] = []  # the first operator names seen, in order (at most MAX_OPERATOR_NAMES)

    # ---------- names ----------

    def key_for(self, provider: str | None) -> str:
        """The key an endpoint is counted under: `user` for the user's own (None), else the operator's provider name;
        a name that is not safe to send (or would read as `user`/`other`), or past the first six, goes to `other`."""
        if provider is None:
            return USER
        if provider in (USER, OTHER) or not NAME.fullmatch(provider):
            return OTHER
        if provider in self._operator_names:
            return provider
        if len(self._operator_names) < MAX_OPERATOR_NAMES:
            self._operator_names.append(provider)
            return provider
        return OTHER

    def _key(self, name: str) -> str:
        return name if name in (USER, OTHER) else self.key_for(name)

    # ---------- counting ----------

    def call_admitted(self, kind: str) -> None:
        if kind in self._calls:
            self._calls[kind] += 1

    def busy(self, kind: str) -> None:
        if kind in self._busy:
            self._busy[kind] += 1

    def not_ready(self) -> None:
        self._not_ready += 1

    def active(self, by_type: dict[str, int]) -> None:
        for kind in KINDS:
            n = by_type.get(kind, 0)
            self._current[kind] = n
            self._peak[kind] = max(self._peak[kind], n)

    def first_audio(self, ms: float) -> None:
        key = bucket(ms)
        self._first_audio[key] = self._first_audio.get(key, 0) + 1

    def provider_time(self, stage: Stage, name: str, ms: float) -> None:
        per_name = self._times[stage].setdefault(self._key(name), {})
        key = bucket(ms)
        per_name[key] = per_name.get(key, 0) + 1

    def provider_failed(self, name: str) -> None:
        key = self._key(name)
        self._failures[key] = self._failures.get(key, 0) + 1

    def cloud_unreachable(self, seconds: float) -> None:
        if seconds > 0:
            self._unreachable_s += seconds

    def outbox_dropped(self, n: int = 1) -> None:
        self._dropped += n

    # ---------- the heartbeat ----------

    def snapshot(self) -> dict[str, Any]:
        """The heartbeat's `stats` (copies; nothing is reset): only the buckets and names with a count."""
        return {
            "calls": dict(self._calls),
            "busy": dict(self._busy),
            "not_ready": self._not_ready,
            "peak": dict(self._peak),
            "first_audio_ms": {k: n for k, n in self._first_audio.items() if n},
            "stt_ms": self._histograms("stt"),
            "tts_ms": self._histograms("tts"),
            "provider_failures": {k: n for k, n in self._failures.items() if n},
            "cloud_unreachable_s": math.ceil(self._unreachable_s),
            "outbox_dropped": self._dropped,
        }

    def _histograms(self, stage: Stage) -> dict[str, dict[str, int]]:
        out = {}
        for name, counts in self._times[stage].items():
            kept = {k: n for k, n in counts.items() if n}
            if kept:
                out[name] = kept
        return out

    def commit(self, sent: dict[str, Any]) -> None:
        """The Cloud took `sent` (a `snapshot`): takes it off the counters, so what came in since the snapshot stays
        for the next heartbeat; `peak` starts again from the calls open now."""
        for mine, theirs in ((self._calls, sent.get("calls")), (self._busy, sent.get("busy"))):
            if isinstance(theirs, dict):
                for kind in KINDS:
                    n = theirs.get(kind, 0)
                    if isinstance(n, int):
                        mine[kind] = max(0, mine[kind] - n)
        self._not_ready = max(0, self._not_ready - _int(sent.get("not_ready")))
        self._peak = dict(self._current)
        _sub(self._first_audio, sent.get("first_audio_ms"))
        for stage, field in (("stt", "stt_ms"), ("tts", "tts_ms")):
            theirs = sent.get(field)
            if isinstance(theirs, dict):
                for name, counts in theirs.items():
                    if name in self._times[stage]:  # type: ignore[index]
                        _sub(self._times[stage][name], counts)  # type: ignore[index]
                        if not self._times[stage][name]:  # type: ignore[index]
                            del self._times[stage][name]  # type: ignore[index]
        _sub(self._failures, sent.get("provider_failures"))
        self._unreachable_s = max(0.0, self._unreachable_s - _int(sent.get("cloud_unreachable_s")))
        self._dropped = max(0, self._dropped - _int(sent.get("outbox_dropped")))


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


# ---------- the voice providers' times (cloud mode) ----------


class _Timed:
    """A provider that is counted: passes every other attribute (`sample_rate`...) to the one it wraps."""

    def __init__(self, inner: Any, stats: NodeStats, name: str) -> None:
        self._inner = inner
        self._stats = stats
        self.stats_name = name

    def __getattr__(self, attr: str) -> Any:
        try:
            inner = self.__dict__["_inner"]
        except KeyError:
            raise AttributeError(attr) from None
        return getattr(inner, attr)

    def failed(self) -> None:
        self._stats.provider_failed(self.stats_name)


class TimedSpeechToText(_Timed):
    """Counts the time of each transcription that succeeds (failures are counted where the call catches them)."""

    async def transcribe(self, wav: bytes, language: str) -> str:
        started = self._stats.clock()
        text = await self._inner.transcribe(wav, language)
        self._stats.provider_time("stt", self.stats_name, (self._stats.clock() - started) * 1000)
        return text


class TimedTextToSpeech(_Timed):
    """Counts the time to the first chunk of audio of each sentence."""

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        started = self._stats.clock()
        first = True
        async for chunk in self._inner.synthesize(text):
            if first:
                first = False
                self._stats.provider_time("tts", self.stats_name, (self._stats.clock() - started) * 1000)
            yield chunk


def note_failure(provider: Any) -> None:
    """A stage failed (error or timeout): counted when its provider is a counted one (cloud mode), else nothing."""
    if isinstance(provider, _Timed):
        provider.failed()
