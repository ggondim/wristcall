"""Call history (design decisions 13 and 16): every call's record, its utterances, and the views of them.

Text is sealed and indexed through the codec here, above the storage, so that every adapter (the Cloud API one of
epic E9 too) receives it already sealed and never holds the key.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Any

from .agents import Agent, retention_seconds
from .config import HistoryConfig
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
    def __init__(
        self, storage: Storage, codec: HistoryCodec, settings: HistoryConfig | None = None, *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.storage = storage
        self.codec = codec
        self.settings = settings or HistoryConfig()
        self._now = now

    def retention_s(self, agent: Agent) -> float | None:
        return retention_seconds(self.settings.effective_days(agent.spec.retention_days))

    async def start(self, agent: Agent, device_id: str | None) -> CallLog:
        """Creates the call's record, open (recording), with the agent's retention in force now."""
        now = self._now()
        retention = self.retention_s(agent)
        record = await self.storage.calls.create(CallRecord(
            id=new_call_id(), user_id=agent.user_id, agent_id=agent.id, device_id=device_id,
            call_type=agent.call_type, status="recording", created_at=now, updated_at=now,
            agent_slug=agent.slug, agent_name=agent.display_name,
            expires_at=None if retention is None else now + retention,
        ))
        return CallLog(self.storage, self.codec, record, now=self._now)

    async def apply_retention(self) -> None:
        """Operator, at startup: the operator's default and ceiling apply to calls made before they changed.
        A deleted agent's calls keep their expiry, within the ceiling (decision H7)."""
        for user in await self.storage.users.list():
            for record in await self.storage.agents.list(user.id):
                agent = Agent.from_record(record)
                await self.storage.calls.set_expiry(user.id, agent.id, self.retention_s(agent))
        if self.settings.max_retention_days is not None:
            await self.storage.calls.cap_expiry(retention_seconds(self.settings.max_retention_days))

    async def purge(self, batch: int = 500) -> int:
        """Operator. Deletes the expired calls a batch at a time, letting calls run in between; returns how many."""
        total, now = 0, self._now()
        while True:
            done = await self.storage.calls.purge_expired(now, batch)
            total += done
            if done < batch:
                return total
            await asyncio.sleep(0)

    async def details(
        self, user_id: str, *, agent_id: str | None = None, since: float | None = None, until: float | None = None,
        terms: list[list[str]] | None = None, page: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Every matching call of the user, newest first, read a page at a time (export, CLI)."""
        before = None
        while True:
            found = await self.storage.calls.list(
                user_id, agent_id=agent_id, since=since, until=until, terms=terms, before=before, limit=page,
            )
            for record in found:
                yield await self.detail(record)
            if len(found) < page:
                return
            # A position, not a call: deleting that call meanwhile (purge, the user) does not end the export early.
            before = (found[-1].created_at, found[-1].id)

    async def _reseal(self, sealed: bool, convert: Callable[[EntryRecord], tuple[str, bool, list[str]]]) -> int:
        done = 0
        while batch := await self.storage.calls.entries_by_seal(sealed, 200):
            for e in batch:
                text, now_sealed, terms = convert(e)
                if await self.storage.calls.replace_entry(replace(e, text=text, sealed=now_sealed), terms):
                    done += 1
        return done

    async def encrypt_all(self) -> int:
        """Operator. Seals the entries kept in the clear (written before the key was set). Returns how many."""
        if not self.codec.encrypted:
            raise HistoryKeyError("set history.encryption_key first")
        await check_key(self.storage, self.codec)

        def seal(e: EntryRecord) -> tuple[str, bool, list[str]]:
            assert e.text is not None
            stored, sealed = self.codec.seal(e.text, aad(e.call_id, e.seq))
            return stored, sealed, self.codec.index_terms(e.text)

        done = await self._reseal(False, seal)
        await self.storage.calls.compact()
        return done

    async def decrypt_all(self) -> int:
        """Operator. Opens every sealed entry with the configured key and forgets the key: afterwards the server runs
        without history.encryption_key. Returns how many."""
        if not self.codec.encrypted:
            raise HistoryKeyError("set history.encryption_key to the key that encrypted the history")
        await check_key(self.storage, self.codec)
        plain = HistoryCodec()

        def unseal(e: EntryRecord) -> tuple[str, bool, list[str]]:
            assert e.text is not None
            text = self.codec.open(e.text, True, aad(e.call_id, e.seq))
            return text, False, plain.index_terms(text)

        done = await self._reseal(True, unseal)
        await self.storage.meta.delete(KEY_ID)
        await self.storage.calls.compact()
        return done

    def _open(self, e: EntryRecord) -> Entry:
        if e.text is None:
            return Entry(seq=e.seq, role=e.role, text=None, error=e.error, at=e.at)
        try:
            return Entry(seq=e.seq, role=e.role, text=self.codec.open(e.text, e.sealed, aad(e.call_id, e.seq)), error=e.error, at=e.at)
        except HistoryKeyError:
            # Sealed with a key this server does not have (check_key refuses that at startup; possible only if the
            # history was decrypted while a server with the key kept writing): shown as unreadable, never a 500.
            log.warning("call %s: utterance %d cannot be decrypted", e.call_id, e.seq)
            return Entry(seq=e.seq, role=e.role, text=None, error="unreadable", at=e.at)

    async def entries(self, record: CallRecord) -> list[Entry]:
        return [self._open(e) for e in await self.storage.calls.entries(record.user_id, record.id)]

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
