"""Redelivery in cloud mode (5.4.1, amendment B1): a failed one-way call's transcript goes again to the agent's webhook,
as `redelivery.py` does self-hosted, but the call lives in the Cloud. The Cloud hands the call to this node
(`POST /v1/node/calls/{id}/redeliver`: it becomes `processing`, with its entries, its agent and a grant), the node
delivers it with the same policy and Idempotency-Key, and reports the outcome only by PATCH through the outbox (never a
`finish`: the call finished long ago). A refusal after the Cloud handed the call over puts it back as it was.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import httpx

from ..agents import ONE_WAY, Agent, build_one_way_providers
from ..config import AppConfig
from ..delivery import DeliveryPolicy, deliver, payload
from ..history import History, user_text
from ..history_api import RedeliveryError
from ..oneway import Background
from ..providers import ProviderError, Webhook
from ..storage import CallRecord, CloudUnavailable
from ..storage.cloud import answer_shape, call_record, entry_records, unexpected_answer
from .calls import CloudCallLog
from .client import raise_for_account
from .node import CloudNode
from .proof import UserProof, require_proof

log = logging.getLogger("wristcall.cloud_node.redelivery")

# The Cloud's refusals (409), each with the copy self-hosted redelivery uses for the same rule.
REFUSALS = {
    "not_one_way": "only one-shot and monologue calls are delivered",
    "call_open": "This call is still open.",
    "busy": "this call is being delivered right now",
    "not_failed": "only a call whose delivery failed can be delivered again",
}
UNAVAILABLE = "the agent's webhook is not available; check its action"
DRAINING = "This server is busy; try again in a minute."


class CloudRedelivery:
    def __init__(
        self,
        node: CloudNode,
        history: History,
        config: AppConfig,
        after_calls: Background,
        http: httpx.AsyncClient,
        *,
        user_http: httpx.AsyncClient | None = None,
        policy: DeliveryPolicy = DeliveryPolicy(),
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._node = node
        self._history = history
        self._config = config
        self._after_calls = after_calls
        self._http = http
        # The user's own webhook goes through this one (limits.public_only, forced in cloud mode).
        self._user_http = user_http
        self._policy = policy
        self._now = now
        self._sleep = sleep

    async def start(self, record: CallRecord) -> CallRecord:
        """The `calls_router`'s redeliver: checks, has the Cloud hand the call over and delivers it in the background
        (`after_calls`). Returns the call as it is now (`processing`). Raises RedeliveryError (409), or the Cloud's
        account errors (`PlanRequired`, `DeviceRevoked`, `CloudUnavailable`)."""
        if record.delivery == "history":  # nothing to ask the Cloud: such a call never had a webhook
            raise RedeliveryError("no_webhook", "This call was saved to the history; it has no webhook to deliver to.")
        if not self._node.accepting:  # starting or draining: a delivery handed over now could be cut at exit
            raise RedeliveryError("busy", DRAINING)
        proof = require_proof(record.user_id)
        body, grant = await self._claim(proof, record.id)
        # From here the Cloud holds the call as this node's `processing`: every way out reports to it with the grant.
        self._node.registry.hold(record.id, grant)
        call_log = CloudCallLog(
            replace(record, status="processing", error=None, finished_at=None), grant, self._history.codec,
            self._node.outbox, self._node.registry, now=self._now, report_finish=False,
        )
        try:
            with answer_shape("POST /v1/node/calls/{id}/redeliver"):
                call = call_record(proof.account, body["call"])
                entries = entry_records(call.id, body["entries"])
                raw_agent = body["agent"]
            call_log.record = call
            agent, webhook, text = self._prepare(proof, call, entries, raw_agent)
        except BaseException:
            await self._put_back(call_log, record)
            raise
        self._after_calls.spawn(self._deliver(call_log, agent, webhook, text))
        return call

    async def _claim(self, proof: UserProof, call_id: str) -> tuple[dict[str, Any], str]:
        """The Cloud's answer (`{call, entries, agent, grant}`) and the grant. Its 409s become RedeliveryError."""
        path = f"/v1/node/calls/{quote(call_id, safe='')}/redeliver"
        resp = await self._node.client.request("POST", path, user=proof)
        raise_for_account(resp)
        if resp.status == 409 and resp.code in REFUSALS:
            raise RedeliveryError(resp.code, REFUSALS[resp.code])
        if resp.status == 404:  # deleted since it was read
            raise RedeliveryError("not_failed", "this call can no longer be delivered")
        if resp.status != 200:
            unexpected_answer(resp, "POST /v1/node/calls/{id}/redeliver")
        grant = resp.body.get("grant") if isinstance(resp.body, dict) else None
        if not isinstance(grant, str) or not grant:
            # Without a grant nothing can be reported: the Cloud fails the call when it finds it orphaned.
            log.warning("Wristcall Cloud handed call %s over without a grant", call_id)
            raise CloudUnavailable("Wristcall Cloud answered the redelivery without a grant")
        return resp.body, grant

    def _prepare(
        self, proof: UserProof, call: CallRecord, entries: list[Any], raw_agent: Any,
    ) -> tuple[Agent, Webhook, str]:
        """The rules self-hosted redelivery checks after the call's own (the Cloud checked those)."""
        text = user_text(self._history.open_entries(call, entries))
        if text is None:
            raise RedeliveryError("no_text", "this call has no transcript to deliver")
        if raw_agent is None:
            raise RedeliveryError("agent_gone", "the agent of this call was deleted")
        # Opened with the proof's account, never one the Cloud names; a spec that does not open is left out.
        opened = self._node.storage.agents.opened(proof.account, [raw_agent])
        if not opened:
            raise RedeliveryError("agent_unavailable", UNAVAILABLE)
        agent = Agent.from_record(opened[0])
        if agent.call_type not in ONE_WAY:
            raise RedeliveryError("not_one_way", "the agent of this call is no longer one-shot or monologue")
        try:
            webhook = build_one_way_providers(self._config, agent.spec, self._http, user_http=self._user_http).webhook
        except ProviderError:
            raise RedeliveryError("agent_unavailable", UNAVAILABLE) from None
        if webhook is None:
            raise RedeliveryError("no_webhook", "This agent has no webhook now; add one to deliver again.")
        return agent, webhook, text

    async def _put_back(self, call_log: CloudCallLog, before: CallRecord) -> None:
        """The call as it was before the Cloud handed it over (`failed`, its error), by a queued PATCH."""
        fields: dict[str, Any] = {"status": "failed", "error": before.error}
        if before.finished_at is not None:
            fields["finished_at"] = before.finished_at
        await call_log.save(finished=before.finished_at is None, **fields)
        self._node.registry.forget(before.id)

    async def _deliver(self, call_log: CloudCallLog, agent: Agent, webhook: Webhook, text: str) -> CallRecord:
        record = call_log.record
        try:
            body = payload(
                call_id=record.id,
                call_type=record.call_type,
                agent={"id": agent.id, "slug": agent.slug, "display_name": agent.display_name},
                language=agent.spec.language,
                text=text,
                started_at=record.created_at,
                ended_at=record.ended_at or record.created_at,
            )
            result = await deliver(webhook, body, idempotency_key=record.id, policy=self._policy, sleep=self._sleep)
            log.info("call %s redelivery: %s", record.id, "delivered" if result.delivered else "failed")
            return await call_log.save(
                status="delivered" if result.delivered else "failed",
                error=None if result.delivered else "delivery_failed",
                attempts=record.attempts + result.attempts,
                last_http_status=result.last_http_status,
                finished=True,
            )
        except asyncio.CancelledError:
            await call_log.save(status="failed", error="interrupted", finished=True)
            raise
        except Exception as e:  # only the kind: an HTTP error's text names the user's webhook URL
            log.error("call %s: unexpected failure while redelivering (%s)", record.id, type(e).__name__)
            return await call_log.save(status="failed", error="delivery_failed", finished=True)
        finally:
            # The PATCHes carry the grant with them; no push follows a redelivery (as self-hosted).
            self._node.registry.forget(record.id)
