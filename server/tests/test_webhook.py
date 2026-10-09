import httpx
import pytest
import respx

from wristcall import __version__
from wristcall.config import ProviderConfig
from wristcall.providers import ProviderError, build_provider
from wristcall.providers.webhook import Webhook, WebhookError

URL = "https://hooks.example/in"


def hook(http, **options) -> Webhook:
    return build_provider("hook", ProviderConfig(type="webhook", url=URL, **options), "webhook", http)


@respx.mock
async def test_posts_json_with_its_headers_and_returns_the_status():
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        status = await hook(http, headers={"Authorization": "Bearer t"}).send(
            {"text": "olá"}, idempotency_key="c_1", timeout_s=5
        )
    assert status == 204
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer t"
    assert req.headers["idempotency-key"] == "c_1"
    assert req.headers["user-agent"] == f"wristcall/{__version__}"
    assert req.headers["content-type"] == "application/json"
    assert req.read() == '{"text":"olá"}'.encode()


@respx.mock
async def test_redirects_are_answers_not_followed():
    respx.post(URL).mock(return_value=httpx.Response(302, headers={"Location": "http://10.0.0.1/"}))
    other = respx.post("http://10.0.0.1/").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        assert await hook(http).send({}, idempotency_key="c", timeout_s=5) == 302
    assert not other.called


@respx.mock
@pytest.mark.parametrize("exc, reason", [(httpx.ConnectError("x"), "connection"), (httpx.ReadTimeout("x"), "timeout")])
async def test_no_answer_is_a_webhook_error(exc, reason):
    respx.post(URL).mock(side_effect=exc)
    async with httpx.AsyncClient() as http:
        with pytest.raises(WebhookError) as e:
            await hook(http).send({}, idempotency_key="c", timeout_s=5)
    assert e.value.reason == reason


@pytest.mark.parametrize(
    "options",
    [
        {"url": "https://h.example/x?k=1"},
        {"url": "https://h.example/x#f"},
        {"url": "mailto:a@b.example"},
        {"url": "https://h.example", "headers": {"Content-Type": "text/plain"}},
        {"url": "https://h.example", "headers": {"Transfer-Encoding": "chunked"}},
        {"url": "https://h.example", "headers": {"bad name": "x"}},
        {"url": "https://h.example", "headers": {f"X-{i}": "v" for i in range(17)}},
        {"url": "https://h.example", "headers": ["X-A"]},
        {"url": "https://h.example", "headers": {"Authorization": "Bearer ção"}},
        {"url": "https://h.example", "headers": {"X-A": "a\tb"}},
    ],
)
async def test_bad_options_fail_at_build(options):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderError):
            build_provider("hook", ProviderConfig(type="webhook", **options), "webhook", http)
