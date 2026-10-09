"""Redelivery of a one-way call whose delivery failed (design decision 12): its kept transcript goes again to the
agent's webhook as it is configured now, with the same policy (three attempts) and the same Idempotency-Key."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from .agents import ONE_WAY, Agent, AgentError, AgentService, build_one_way_providers
from .config import AppConfig
from .delivery import DeliveryPolicy, deliver, payload
from .history import CallLog, History, user_text
from .history_api import RedeliveryError
from .oneway import Background
from .providers import ProviderError, Webhook
from .storage import CallRecord

log = logging.getLogger("wristcall.redelivery")

# What a failed call may be redelivered after: the webhook refused, or the server stopped mid-delivery.
REDELIVERABLE = frozenset({"delivery_failed", "interrupted"})


class Redelivery:
    def __init__(
        self,
        history: History,
        agents: AgentService,
        config: AppConfig,
        http: httpx.AsyncClient,
        *,
        policy: DeliveryPolicy = DeliveryPolicy(),
        background: Background | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._history = history
        self._agents = agents
        self._config = config
        self._http = http
        self._policy = policy
        self._background = background
        self._now = now
        self._sleep = sleep
        self._busy: set[str] = set()

    async def _prepare(self, record: CallRecord) -> tuple[Agent, Webhook, str]:
        if record.call_type not in ONE_WAY:
            raise RedeliveryError("not_one_way", "only one-shot and monologue calls are delivered")
        if record.status == "processing" or record.id in self._busy:
            raise RedeliveryError("busy", "this call is being delivered right now")
        if record.status != "failed" or record.error not in REDELIVERABLE:
            raise RedeliveryError("not_failed", "only a call whose delivery failed can be delivered again")
        text = user_text(await self._history.entries(record))
        if text is None:
            raise RedeliveryError("no_text", "this call has no transcript to deliver")
        try:
            agent = await self._agents.get(record.user_id, record.agent_id)
        except AgentError:
            raise RedeliveryError("agent_gone", "the agent of this call was deleted") from None
        if agent.call_type not in ONE_WAY:
            raise RedeliveryError("not_one_way", "the agent of this call is no longer one-shot or monologue")
        try:
            webhook = build_one_way_providers(self._config, agent.spec, self._http).webhook
        except ProviderError:
            raise RedeliveryError("agent_unavailable", "the agent's webhook is not available; check its action") from None
        return agent, webhook, text

    async def _begin(self, record: CallRecord) -> tuple[CallLog, Agent, Webhook, str]:
        agent, webhook, text = await self._prepare(record)
        self._busy.add(record.id)
        call_log = CallLog(self._history.storage, self._history.codec, record, now=self._now)
        await call_log.save(status="processing", error=None, finished_at=None)
        return call_log, agent, webhook, text

    async def start(self, record: CallRecord) -> CallRecord:
        """API: checks, marks the call processing and delivers in the background. Raises RedeliveryError."""
        call_log, agent, webhook, text = await self._begin(record)
        task = self._deliver(call_log, agent, webhook, text)
        if self._background is not None:
            self._background.spawn(task)
        else:
            asyncio.create_task(task)
        return call_log.record

    async def run(self, record: CallRecord) -> CallRecord:
        """CLI: the same, waiting for the outcome."""
        return await self._deliver(*await self._begin(record))

    async def _deliver(self, call_log: CallLog, agent: Agent, webhook: Webhook, text: str) -> CallRecord:
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
        except Exception:
            log.exception("call %s: unexpected failure while redelivering", record.id)
            return await call_log.save(status="failed", error="delivery_failed", finished=True)
        finally:
            self._busy.discard(record.id)
