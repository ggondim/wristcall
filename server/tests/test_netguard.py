import asyncio

import httpcore
import httpx
import pytest
from conftest import fake_resolver

from wristcall.netguard import (
    NotPublic,
    PublicOnlyBackend,
    UrlNotPublic,
    check_public_url,
    is_public_ip,
    public_http_client,
    resolve_public,
)

PUBLIC = {"api.example.com": ["93.184.216.34"]}


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("93.184.216.34", True),
        ("2606:4700:4700::1111", True),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("100.100.100.100", False),
        ("0.0.0.0", False),
        ("198.18.0.1", False),
        ("255.255.255.255", False),
        ("224.0.0.1", False),
        ("233.252.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fe80::1%en0", False),
        ("fd7a:115c:a1e0::1", False),
        ("::ffff:10.0.0.1", False),
        ("64:ff9b::a00:1", False),
        ("2002:a00:1::", False),
        ("::a00:1", False),
    ],
)
def test_is_public_ip(address, public):
    assert is_public_ip(address) is public


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.example.com/v1", ("api.example.com", 443)),
        ("https://Hooks.Example.com:8443/x", ("hooks.example.com", 8443)),
        ("https://93.184.216.34/", ("93.184.216.34", 443)),
    ],
)
def test_check_public_url_accepts(url, expected):
    assert check_public_url(url) == expected


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://api.example.com", "api.example.com"),
        ("https://localhost/x", "localhost"),
        ("https://LOCALHOST/", "localhost"),
        ("https://foo.localhost/", "foo.localhost"),
        ("https://speaches:8000/v1", "speaches"),
        ("https://speaches./v1", "speaches"),
        ("https://printer.local/", "printer.local"),
        ("https://x.internal/", "x.internal"),
        ("https://router.lan/", "router.lan"),
        ("https://nas.home.arpa/", "nas.home.arpa"),
        ("https://10.0.0.1/", "10.0.0.1"),
        ("https://[::1]/", "::1"),
        ("https://[64:ff9b::a00:1]/", "64:ff9b::a00:1"),
    ],
)
def test_check_public_url_refuses(url, host):
    with pytest.raises(NotPublic) as exc:
        check_public_url(url)
    message = str(exc.value)
    assert message
    assert host not in message.lower()


async def test_resolve_public():
    assert await resolve_public("api.example.com", 443, fake_resolver(PUBLIC)) == ["93.184.216.34"]

    mixed = fake_resolver({"api.example.com": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(NotPublic, match="^the host resolves to a private or internal address$"):
        await resolve_public("api.example.com", 443, mixed)

    for resolver in (fake_resolver({"api.example.com": []}), fake_resolver({})):
        with pytest.raises(NotPublic, match="^the host does not resolve$"):
            await resolve_public("api.example.com", 443, resolver)

    async def slow(host: str, port: int) -> list[str]:
        await asyncio.sleep(1)
        return ["93.184.216.34"]

    with pytest.raises(NotPublic, match="^the host does not resolve$"):
        await resolve_public("api.example.com", 443, slow, timeout_s=0.01)


async def test_resolve_public_takes_ip_literals_without_dns():
    assert await resolve_public("93.184.216.34", 443, fake_resolver({})) == ["93.184.216.34"]
    with pytest.raises(NotPublic, match="^the host is a private or internal address$"):
        await resolve_public("10.0.0.1", 443, fake_resolver({}))


class FakeInner(httpcore.AsyncNetworkBackend):
    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail = fail
        self.stream = object()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, port))
        if host in self.fail:
            raise httpcore.ConnectError(f"unreachable {host}")
        return self.stream

    async def sleep(self, seconds):
        self.slept = seconds


async def test_backend_connects_to_the_checked_address():
    inner = FakeInner()
    backend = PublicOnlyBackend(fake_resolver(PUBLIC), inner)
    stream = await backend.connect_tcp("api.example.com", 443)
    assert stream is inner.stream
    assert inner.calls == [("93.184.216.34", 443)]


async def test_backend_tries_each_checked_address():
    inner = FakeInner(fail={"2606:4700:4700::1111"})
    table = {"api.example.com": ["2606:4700:4700::1111", "93.184.216.34"]}
    backend = PublicOnlyBackend(fake_resolver(table), inner)
    stream = await backend.connect_tcp("api.example.com", 443)
    assert stream is inner.stream
    assert inner.calls == [("2606:4700:4700::1111", 443), ("93.184.216.34", 443)]


async def test_backend_raises_the_last_connect_error():
    inner = FakeInner(fail={"2606:4700:4700::1111", "93.184.216.34"})
    table = {"api.example.com": ["2606:4700:4700::1111", "93.184.216.34"]}
    backend = PublicOnlyBackend(fake_resolver(table), inner)
    with pytest.raises(httpcore.ConnectError, match="93.184.216.34"):
        await backend.connect_tcp("api.example.com", 443)
    assert len(inner.calls) == 2


async def test_backend_refuses_private_addresses():
    inner = FakeInner()
    backend = PublicOnlyBackend(fake_resolver({"api.example.com": ["10.0.0.1"]}), inner)
    with pytest.raises(UrlNotPublic, match="^the host resolves to a private or internal address$"):
        await backend.connect_tcp("api.example.com", 443)
    assert inner.calls == []


async def test_backend_refuses_unix_sockets_and_passes_sleep():
    inner = FakeInner()
    backend = PublicOnlyBackend(fake_resolver(PUBLIC), inner)
    with pytest.raises(UrlNotPublic):
        await backend.connect_unix_socket("/var/run/docker.sock")
    await backend.sleep(0.5)
    assert inner.slept == 0.5


async def test_public_client_refuses_before_connecting():
    client = public_http_client(fake_resolver({"internal.example.com": ["10.0.0.9"]}), timeout=httpx.Timeout(5.0))
    async with client:
        with pytest.raises(UrlNotPublic) as exc:
            await client.get("https://internal.example.com/")
    assert isinstance(exc.value, httpx.ConnectError)
    assert client._transport._pool._max_connections == 100


async def test_public_client_refuses_plain_http_before_resolving():
    calls: list[str] = []
    inner = fake_resolver(PUBLIC)

    async def resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        return await inner(host, port)

    client = public_http_client(resolver, timeout=httpx.Timeout(0.5))
    async with client:
        with pytest.raises(UrlNotPublic, match="^the URL must use https$"):
            await client.get("http://api.example.com/")
    assert calls == []


async def test_public_client_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.1:3128")
    client = public_http_client(fake_resolver({"internal.example.com": ["10.0.0.9"]}), timeout=httpx.Timeout(5.0))
    assert client.trust_env is False
    assert client.follow_redirects is False
    assert client._mounts == {}
    async with client:
        with pytest.raises(UrlNotPublic):
            await client.get("https://internal.example.com/")
