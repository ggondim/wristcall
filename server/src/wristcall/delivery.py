"""Delivery of a one-way call's transcript to the agent's webhook (design decisions 11 and 12).

Any 2xx is delivered. Three attempts (one now, two quick retries) within about a minute: with the defaults,
the worst case is 15 + 3 + 15 + 6 + 15 = 54 s. Every attempt carries the call id as Idempotency-Key, so a
receiver that answered too late to count can drop the repeat.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .providers import ProviderError, Webhook
from .providers.webhook import WebhookError

log = logging.getLogger("wristcall.delivery")

PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class DeliveryPolicy:
    attempt_timeout_s: float = 15.0
    retry_delays_s: tuple[float, ...] = (3.0, 6.0)


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    attempts: int
    last_http_status: int | None
    # None when delivered; else why the last attempt failed: "http_status" (not 2xx), "timeout" or "connection".
    error: str | None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def payload(
    *, call_id: str, call_type: str, agent: dict[str, str], language: str, text: str, started_at: float, ended_at: float
) -> dict[str, Any]:
    """The JSON body the webhook receives. Fields may be added within version 1; receivers ignore unknown ones."""
    return {
        "event": "call.completed",
        "version": PAYLOAD_VERSION,
        "call_id": call_id,
        "call_type": call_type,
        "agent": agent,
        "language": language,
        "text": text,
        "started_at": iso(started_at),
        "ended_at": iso(ended_at),
    }


async def deliver(
    webhook: Webhook,
    body: dict[str, Any],
    *,
    idempotency_key: str,
    policy: DeliveryPolicy = DeliveryPolicy(),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DeliveryResult:
    status: int | None = None
    error: str | None = None
    attempts = 0
    for delay in (None, *policy.retry_delays_s):
        if delay is not None:
            await sleep(delay)
        attempts += 1
        try:
            status = await webhook.send(body, idempotency_key=idempotency_key, timeout_s=policy.attempt_timeout_s)
        except WebhookError as e:
            status, error = None, e.reason
        except ProviderError:
            status, error = None, "connection"
        else:
            if 200 <= status < 300:
                log.info("call %s delivered (attempt %d, HTTP %d)", idempotency_key, attempts, status)
                return DeliveryResult(True, attempts, status, None)
            error = "http_status"
        # Never the URL or the text: both belong to the user.
        log.warning("call %s: delivery attempt %d failed (%s)", idempotency_key, attempts, status or error)
    return DeliveryResult(False, attempts, status, error)
