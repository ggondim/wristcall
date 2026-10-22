"""Calls in cloud mode: what a call writes to the Cloud while it happens (`CloudCallLog`) and the push that tells the
watch how a one-way call ended (`CloudPushNotifier`). Both go through the outbox with the call grant, never straight
to the Cloud, so a Cloud that is down mid-call delays the writes and the call goes on.
"""

import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..agents import ONE_WAY
from ..history import aad_scoped
from ..history_codec import HistoryCodec
from ..push import MAX_BODY, MAX_TITLE, call_text
from ..storage import AgentRecord, CallRecord, CloudUnavailable
from ..storage.cloud import PATH_REF
from .outbox import CallRegistry, Outbox, OutboxItem
from .proof import UserProof

if TYPE_CHECKING:
    from .node import CloudNode

log = logging.getLogger("wristcall.cloud_node.calls")

# The record fields the Cloud takes in `PATCH /v1/node/calls/{id}`.
PATCH_FIELDS = ("status", "error", "attempts", "last_http_status", "ended_at", "finished_at", "delivery")
# A save with one of these statuses ends the call on the Cloud (`finish`, once): the account's call slot is freed and
# the seconds counted. A one-way call reaches `processing` at hang-up and its outcome follows as a PATCH (D21).
FINISH_STATUSES = frozenset({"processing", "ended", "empty", "delivered", "failed"})
TERM_HASH = re.compile(r"^x[0-9a-f]{16}$")
MAX_TERM_HASHES = 2000

# What a refused call tells the user (the WebSocket `error` message).
PLAN_REQUIRED = "This account has no Wristcall Cloud plan."
QUOTA_EXCEEDED = "You've used this month's call minutes."
CLOUD_UNREACHABLE = "Wristcall Cloud is unavailable; try again."
NEEDS_VOICE_URL = "This agent needs your own transcription and voice addresses."
TOO_MANY_CALLS = "Another call is still open on this account."
BUSY = "This server is busy; try again in a minute."


@dataclass(frozen=True)
class OpenedCall:
    """A call the Cloud opened: its id, the grant that writes it, and how long it may last (`max_s`, already within
    the account's remaining minutes `quota_s`)."""

    call_id: str
    grant: str = field(repr=False)  # writes this call on the Cloud; never logged
    agent: AgentRecord = field(repr=False)
    plan: str | None
    quota_s: int
    max_s: int
    started_at: float


class CallRefused(Exception):
    """The call cannot start. `code`: plan_required, quota_exceeded, busy, unknown_profile, agent_unavailable (the
    WebSocket error codes) or unauthorized (the device was removed: the WebSocket closes 4401); `reason` says more."""

    def __init__(self, code: str, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


def check_plan(plan: str | None, agent: AgentRecord) -> None:
    """The plan rules a call follows (spec 7.1): no plan, no call; the `custom` plan brings its own transcription and
    voice, so an agent on the operator's (`{"provider": ...}`) cannot be called."""
    if plan is None:
        raise CallRefused("plan_required", PLAN_REQUIRED)
    if plan == "custom" and any(_operator_endpoint(agent.spec.get(stage)) for stage in ("stt", "tts")):
        raise CallRefused("agent_unavailable", NEEDS_VOICE_URL, "needs_voice_url")


def call_deadline(opened: OpenedCall, local_s: int | None) -> tuple[int, bool]:
    """When the call is cut by the wall clock (D12), in seconds from ready: the Cloud's `max_s` or the node's own
    limit for the call type, whichever is shorter; and whether it is the month's minutes that run out then (the user
    is told `quota_exceeded`) rather than the call's limit (`call.captured`, reason `limit`)."""
    seconds = opened.max_s if local_s is None else min(opened.max_s, local_s)
    return seconds, opened.quota_s <= seconds


def _operator_endpoint(endpoint: Any) -> bool:
    return isinstance(endpoint, dict) and "provider" in endpoint


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def open_call(
    node: "CloudNode", proof: UserProof, agent: AgentRecord, *, delivery: str | None, request_id: str
) -> OpenedCall:
    """`POST /v1/node/calls` for the proof's user. A Cloud that does not answer is asked once more with the same
    `request_id` (the Cloud answers a replay with the call it already opened, 200). Raises `CallRefused`."""
    body = {"agent": agent.id, "call_type": agent.call_type, "request_id": request_id, "delivery": delivery}
    for attempt in (1, 2):
        try:
            resp = await node.client.request("POST", "/v1/node/calls", user=proof, json=body)
            if resp.status in (200, 201):
                return _opened(resp.body, agent)
            if resp.status in (401, 429) or resp.status >= 500:
                raise CloudUnavailable(f"Wristcall Cloud answered {resp.status}")
        except CloudUnavailable:
            if attempt == 1:
                log.info("opening a call: Wristcall Cloud did not answer; asking once more")
                continue
            log.warning("opening a call: Wristcall Cloud unavailable")
            raise CallRefused("agent_unavailable", CLOUD_UNREACHABLE, "cloud_unreachable") from None
        raise _refusal(resp.status, resp.code, agent)
    raise AssertionError("unreachable")  # pragma: no cover


def _refusal(status: int, code: str | None, agent: AgentRecord) -> CallRefused:
    if status == 402 and code == "plan_required":
        return CallRefused("plan_required", PLAN_REQUIRED)
    if status == 402 and code == "quota_exceeded":
        return CallRefused("quota_exceeded", QUOTA_EXCEEDED)
    if status == 403 and code == "device_revoked":
        return CallRefused("unauthorized", "this device was removed from the account")
    if status == 409 and code == "too_many_calls":
        return CallRefused("busy", TOO_MANY_CALLS, "too_many_calls")
    if status == 404:
        return CallRefused("unknown_profile", f"unknown agent: {agent.slug}")
    log.warning("opening a call: Wristcall Cloud answered %d (%s)", status, code or "no code")
    return CallRefused("agent_unavailable", CLOUD_UNREACHABLE, "cloud_unreachable")


def _opened(body: Any, agent: AgentRecord) -> OpenedCall:
    """The call the Cloud opened. The agent stays the one the node checked (its providers are already built)."""
    try:
        plan = body["plan"]
        call_id, grant, quota_s, max_s, started_at = (
            body["call_id"], body["grant"], body["quota_s"], body["max_s"], body["started_at"]
        )
        # The id goes into request paths and the registry: only what the Cloud's own paths accept.
        if not (isinstance(call_id, str) and PATH_REF.fullmatch(call_id) and isinstance(grant, str) and grant):
            raise TypeError("call_id or grant")
        if not (_whole(quota_s) and _whole(max_s)):
            raise TypeError("quota_s or max_s")
        if isinstance(started_at, bool) or not isinstance(started_at, int | float):
            raise TypeError("started_at")
        if plan is not None and not (isinstance(plan, dict) and isinstance(plan.get("plan"), str)):
            raise TypeError("plan")
    except (KeyError, TypeError):
        log.warning("Wristcall Cloud answered POST /v1/node/calls without the expected fields")
        raise CloudUnavailable("Wristcall Cloud answered POST /v1/node/calls without the expected fields") from None
    return OpenedCall(
        call_id=call_id, grant=grant, agent=agent, plan=plan["plan"] if plan else None, quota_s=quota_s, max_s=max_s,
        started_at=float(started_at),
    )


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
        """Tells the device that made a one-way call how it ended. Called once per call: the call's grant is
        forgotten whether or not a push goes out."""
        grant = self._registry.grant(record.id)
        text = call_text(record) if record.call_type in ONE_WAY else None
        if text is None:
            self._registry.forget(record.id)
            return
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
