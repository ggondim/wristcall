"""Calls in cloud mode: what a call writes to the Cloud while it happens (`CloudCallLog`) and the push that tells the
watch how a one-way call ended (`CloudPushNotifier`). Both go through the outbox with the call grant, never straight
to the Cloud, so a Cloud that is down mid-call delays the writes and the call goes on.
"""

import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..agents import ONE_WAY
from ..history import aad_scoped
from ..history_codec import HistoryCodec
from ..push import MAX_BODY, MAX_TITLE, call_text
from ..storage import CallRecord
from .outbox import CallRegistry, Outbox, OutboxItem

log = logging.getLogger("wristcall.cloud_node.calls")

# The record fields the Cloud takes in `PATCH /v1/node/calls/{id}`.
PATCH_FIELDS = ("status", "error", "attempts", "last_http_status", "ended_at", "finished_at", "delivery")
# A save with one of these statuses ends the call on the Cloud (`finish`, once): the account's call slot is freed and
# the seconds counted. A one-way call reaches `processing` at hang-up and its outcome follows as a PATCH (D21).
FINISH_STATUSES = frozenset({"processing", "ended", "empty", "delivered", "failed"})
TERM_HASH = re.compile(r"^x[0-9a-f]{16}$")
MAX_TERM_HASHES = 2000


class CloudCallLog:
    """`history.CallLog` for a call opened on the Cloud: the same surface (`record`, `count`, `add`, `save`), writing
    through the outbox with the call's grant. Text is sealed bound to the account, the call and the position
    (`aad_scoped`) and indexed with the account's own index key.

    `report_finish=False` (a redelivery: the call was finished long ago) never sends `finish`, only PATCHes.
    """

    def __init__(
        self,
        record: CallRecord,
        grant: str,
        codec: HistoryCodec,
        outbox: Outbox,
        registry: CallRegistry,
        *,
        now: Callable[[], float] = time.time,
        report_finish: bool = True,
    ) -> None:
        if not codec.encrypted:  # the Cloud takes only sealed text; cloud mode requires history.encryption_key
            raise ValueError("cloud-mode call history needs history.encryption_key")
        self.record = record
        self._grant = grant
        self._codec = codec.for_account(record.user_id)
        self._outbox = outbox
        self._registry = registry
        self._now = now
        self._report_finish = report_finish
        self._finish_sent = False
        self._seq = 0
        self._ready_at: float | None = None

    @property
    def count(self) -> int:
        """Utterances recorded so far (tried, even if the write failed)."""
        return self._seq

    def mark_ready(self, at: float) -> None:
        """When the call became ready (`session.ready`): the Cloud bills from here (P12)."""
        self._ready_at = at

    @property
    def seconds(self) -> int:
        """Billed seconds, rounded up: from ready to the end. 0 when the call never became ready or has not ended."""
        if self._ready_at is None or self.record.ended_at is None:
            return 0
        return max(0, math.ceil(self.record.ended_at - self._ready_at))

    def _put(self, kind: str, path: str, body: dict[str, Any]) -> None:
        self._outbox.put(OutboxItem(self.record.id, kind, path, body, self._grant))  # type: ignore[arg-type]

    async def add(self, role: str, text: str | None, error: str | None = None) -> None:
        """Appends an utterance. Never raises (decision H9): a call goes on even if its history cannot be written."""
        seq = self._seq
        self._seq += 1
        try:
            if text:
                stored, sealed = self._codec.seal(text, aad_scoped(self.record.user_id, self.record.id, seq))
                hashes = [h for h in self._codec.index_terms(text) if TERM_HASH.fullmatch(h)][:MAX_TERM_HASHES]
            else:
                stored, sealed, hashes = None, False, []
            entry = {"seq": seq, "role": role, "text": stored, "sealed": sealed, "error": error, "at": self._now(),
                     "term_hashes": hashes}
            self._put("entries", f"/v1/node/calls/{self.record.id}/entries", {"entries": [entry]})
        except Exception:
            log.exception("call %s: could not record utterance %d", self.record.id, seq)

    async def save(self, *, finished: bool = False, **fields: Any) -> CallRecord:
        """Updates the record. The first save that ends the call (FINISH_STATUSES) queues the other fields it carries as
        a PATCH, then `finish`; every other save is a PATCH of the fields the Cloud takes."""
        now = self._now()
        stamps = {"finished_at": now} if finished else {}
        self.record = replace(self.record, **fields, updated_at=now, **stamps)
        sent = {name: getattr(self.record, name) for name in PATCH_FIELDS if name in fields or name in stamps}
        path = f"/v1/node/calls/{self.record.id}"
        if self._report_finish and not self._finish_sent and self.record.status in FINISH_STATUSES:
            self._finish_sent = True
            rest = {k: v for k, v in sent.items() if k not in ("status", "error")}
            if rest:
                self._put("patch", path, rest)
            finish: dict[str, Any] = {"status": self.record.status, "seconds": self.seconds}
            if self.record.error is not None:
                finish["error"] = self.record.error
            self._put("finish", f"{path}/finish", finish)
        elif sent:
            self._put("patch", path, sent)
        return self.record


class CloudPushNotifier:
    """Cloud mode's `PushNotifier.call_finished`: the push goes through the outbox with the call grant, and the
    Cloud finds the device's push key. Conversations get no push."""

    def __init__(self, outbox: Outbox, registry: CallRegistry) -> None:
        self._outbox = outbox
        self._registry = registry

    async def call_finished(self, record: CallRecord) -> None:
        """Tells the device that made a one-way call how it ended."""
        if record.call_type not in ONE_WAY:
            return
        text = call_text(record)
        if text is None:
            return
        grant = self._registry.grant(record.id)
        if grant is None:
            log.info("call %s: no grant left for its push; not sent", record.id)
            return
        title, body = text
        message = {
            "event": "call.finished", "title": title[:MAX_TITLE], "body": body[:MAX_BODY],
            "data": {"call_id": record.id, "status": record.status, "error": record.error, "agent_id": record.agent_id},
            "collapse_id": record.id, "call": record.id,
        }
        self._outbox.put(OutboxItem(record.id, "push", "/v1/node/push", message, grant))  # forgets the grant
