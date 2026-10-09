"""Call history (design decisions 13 and 16): every call's record, its utterances, and the views of them.

Text is sealed and indexed through the codec here, above the storage, so that every adapter (the Cloud API one of
epic E9 too) receives it already sealed and never holds the key.
"""

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .agents import Agent
from .history_codec import HistoryCodec, HistoryKeyError
from .storage import CallRecord, EntryRecord, Storage

log = logging.getLogger("wristcall.history")


KEY_ID = "history_key_id"  # meta: names the key that sealed the history (never the key itself)


async def check_key(storage: Storage, codec: HistoryCodec) -> None:
    """Refuses a key other than the one that sealed the history, or no key once it is sealed (decision H4).

    The first start with a key records which key it is. Raises HistoryKeyError.
    """
    stored = await storage.meta.get(KEY_ID)
    if stored is None:
        if codec.key_id is not None:
            await storage.meta.set(KEY_ID, codec.key_id)
        return
    if codec.key_id is None:
        raise HistoryKeyError(
            "the call history is encrypted: set history.encryption_key (or run `wristcall history decrypt` with it first)"
        )
    if codec.key_id != stored:
        raise HistoryKeyError("history.encryption_key is not the key that encrypted the call history")


def new_call_id() -> str:
    return f"c_{secrets.token_hex(8)}"


def aad(call_id: str, seq: int) -> str:
    """What a sealed text is bound to: its call and position."""
    return f"{call_id}:{seq}"


@dataclass(frozen=True)
class Entry:
    """An utterance as the owner reads it (opened)."""

    seq: int
    role: str
    text: str | None
    error: str | None
    at: float


class CallLog:
    """Writes one call to the history while it happens."""

    def __init__(
        self, storage: Storage, codec: HistoryCodec, record: CallRecord, *, now: Callable[[], float] = time.time
    ) -> None:
        self.record = record
        self._storage = storage
        self._codec = codec
        self._now = now
        self._seq = 0

    @property
    def count(self) -> int:
        """Utterances recorded so far (tried, even if the write failed)."""
        return self._seq

    async def add(self, role: str, text: str | None, error: str | None = None) -> None:
        """Appends an utterance. Never raises (decision H9): a call goes on even if its history cannot be written."""
        seq = self._seq
        self._seq += 1
        try:
            stored, sealed = self._codec.seal(text, aad(self.record.id, seq)) if text else (None, False)
            entry = EntryRecord(
                call_id=self.record.id, seq=seq, role=role, text=stored, sealed=sealed, error=error, at=self._now()
            )
            if not await self._storage.calls.add_entry(self.record.user_id, entry, self._codec.index_terms(text or "")):
                log.info("call %s: gone, utterance not recorded", self.record.id)
        except Exception:
            log.exception("call %s: could not record utterance %d", self.record.id, seq)

    async def save(self, *, finished: bool = False, **fields: Any) -> CallRecord:
        now = self._now()
        self.record = replace(self.record, **fields, updated_at=now, **({"finished_at": now} if finished else {}))
        try:
            return await self._storage.calls.save(self.record)
        except KeyError:
            # The user (or this call) was deleted meanwhile: nothing left to update.
            log.info("call %s: gone while recording", self.record.id)
            return self.record


class History:
    def __init__(self, storage: Storage, codec: HistoryCodec, *, now: Callable[[], float] = time.time) -> None:
        self.storage = storage
        self.codec = codec
        self._now = now

    async def start(self, agent: Agent, device_id: str | None) -> CallLog:
        """Creates the call's record, open (recording)."""
        now = self._now()
        record = await self.storage.calls.create(CallRecord(
            id=new_call_id(), user_id=agent.user_id, agent_id=agent.id, device_id=device_id,
            call_type=agent.call_type, status="recording", created_at=now, updated_at=now,
            agent_slug=agent.slug, agent_name=agent.display_name,
        ))
        return CallLog(self.storage, self.codec, record, now=self._now)

    async def entries(self, record: CallRecord) -> list[Entry]:
        """Raises HistoryKeyError if a sealed text cannot be opened."""
        return [
            Entry(
                seq=e.seq, role=e.role, error=e.error, at=e.at,
                text=None if e.text is None else self.codec.open(e.text, e.sealed, aad(e.call_id, e.seq)),
            )
            for e in await self.storage.calls.entries(record.user_id, record.id)
        ]

    async def detail(self, record: CallRecord) -> dict[str, Any]:
        return call_detail(record, await self.entries(record))


def user_text(entries: list[Entry]) -> str | None:
    """What the user said, joined: the transcript a one-way call delivers."""
    return " ".join(e.text for e in entries if e.role == "user" and e.text) or None


def call_detail(r: CallRecord, entries: list[Entry]) -> dict[str, Any]:
    """`GET /v1/calls/{id}`: the 0.4.0 fields (what a client shows after hanging up) plus the agent and the entries."""
    return {
        "id": r.id,
        "agent_id": r.agent_id,
        "call_type": r.call_type,
        "status": r.status,
        "error": r.error,
        "text": user_text(entries),
        "attempts": r.attempts,
        "last_http_status": r.last_http_status,
        "created_at": r.created_at,
        "ended_at": r.ended_at,
        "finished_at": r.finished_at,
        "agent": {"id": r.agent_id, "slug": r.agent_slug, "display_name": r.agent_name},
        "expires_at": r.expires_at,
        "entries": [{"role": e.role, "text": e.text, "error": e.error, "at": e.at} for e in entries],
    }
