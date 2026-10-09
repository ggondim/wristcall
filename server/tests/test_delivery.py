import httpx
import pytest
import respx

from wristcall.config import ProviderConfig
from wristcall.delivery import DeliveryPolicy, DeliveryResult, deliver, iso, payload
from wristcall.providers import build_provider
from wristcall.providers.webhook import WebhookError

URL = "https://hooks.example/in"


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


class Script:
    """A webhook that answers each attempt from a list: an HTTP status or an exception."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[dict, str, float]] = []

    async def send(self, body, *, idempotency_key, timeout_s):
        self.sent.append((body, idempotency_key, timeout_s))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_first_2xx_is_delivered():
    hook, sleeps = Script(202), Sleeps()
    assert await deliver(hook, {"a": 1}, idempotency_key="c_1", sleep=sleeps) == DeliveryResult(True, 1, 202, None)
    assert hook.sent == [({"a": 1}, "c_1", 15.0)] and sleeps.calls == []


async def test_retries_twice_then_succeeds():
    hook, sleeps = Script(500, WebhookError("timeout"), 200), Sleeps()
    assert await deliver(hook, {}, idempotency_key="c", sleep=sleeps) == DeliveryResult(True, 3, 200, None)
    assert sleeps.calls == [3.0, 6.0]
    assert [k for _, k, _ in hook.sent] == ["c", "c", "c"]


@pytest.mark.parametrize(
    "answers, result",
    [
        ((500, 503, 404), DeliveryResult(False, 3, 404, "http_status")),
        ((500, 500, WebhookError("timeout")), DeliveryResult(False, 3, None, "timeout")),
        ((WebhookError("connection"),) * 3, DeliveryResult(False, 3, None, "connection")),
        ((301, 302, 307), DeliveryResult(False, 3, 307, "http_status")),
    ],
)
async def test_three_failures_give_up(answers, result):
    assert await deliver(Script(*answers), {}, idempotency_key="c", sleep=Sleeps()) == result


async def test_policy_bounds_attempts_and_the_worst_case_fits_a_minute():
    policy = DeliveryPolicy()
    worst = policy.attempt_timeout_s * (1 + len(policy.retry_delays_s)) + sum(policy.retry_delays_s)
    assert worst <= 60
    hook = Script(500, 500)
    result = await deliver(hook, {}, idempotency_key="c", policy=DeliveryPolicy(1.0, (0.5,)), sleep=Sleeps())
    assert result.attempts == 2 and hook.sent[0][2] == 1.0


@respx.mock
async def test_a_slow_webhook_is_cut_at_the_attempt_timeout():
    async def slow(request):
        import asyncio
        await asyncio.sleep(5)
        return httpx.Response(200)

    respx.post(URL).mock(side_effect=slow)
    async with httpx.AsyncClient() as http:
        hook = build_provider("h", ProviderConfig(type="webhook", url=URL), "webhook", http)
        result = await deliver(hook, {}, idempotency_key="c", policy=DeliveryPolicy(0.05, (0.0,)))
    assert result == DeliveryResult(False, 2, None, "timeout")


def test_payload_shape():
    body = payload(
        call_id="c_1", call_type="one-shot", agent={"id": "ag_1", "slug": "note", "display_name": "Note"},
        language="pt", text="comprar leite", started_at=0.0, ended_at=61.5,
    )
    assert body == {
        "event": "call.completed", "version": 1, "call_id": "c_1", "call_type": "one-shot",
        "agent": {"id": "ag_1", "slug": "note", "display_name": "Note"}, "language": "pt", "text": "comprar leite",
        "started_at": "1970-01-01T00:00:00Z", "ended_at": "1970-01-01T00:01:01Z",
    }
    assert iso(1_760_000_000) == "2025-10-09T08:53:20Z"
