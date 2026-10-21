"""Public URLs only: the rule for a user's own endpoints (spec 7.4) and an HTTP client that enforces it.

A URL is public when it uses https, its host is not a local or internal name, and every address the host resolves
to is a global unicast address (an IPv4 address embedded in IPv6 counts too). `check_public_url` checks syntax and
name when a URL is saved; `public_http_client` checks every address again each time it opens a connection and
connects to the address it checked, so a name that resolves somewhere else later (DNS rebinding) gets nowhere. TLS
still validates the certificate against the name. Errors never carry the URL, host or address.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

import httpcore
import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]

NOT_HTTPS = "the URL must use https"
LOCAL_NAME = "the host is a local or internal name"
PRIVATE_ADDRESS = "the host is a private or internal address"
NO_ADDRESS = "the host does not resolve"
RESOLVES_PRIVATE = "the host resolves to a private or internal address"

LOCAL_NAMES = frozenset({"localhost"})
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Deprecated IPv4-compatible IPv6 (RFC 4291 2.5.5.1); nothing public uses it, so the whole block is refused.
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


class NotPublic(Exception):
    """A URL or host that is not public; the message is one of the fixed phrases above."""


class UrlNotPublic(httpx.ConnectError):
    """Raised by the public client when it refuses a connection: an httpx.HTTPError like any failed connection."""


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6:
        if ip in IPV4_COMPATIBLE:
            return False
        embedded = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
        if ip in NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF))
        if any(e is not None and not is_public_ip(str(e)) for e in embedded):
            return False
    return ip.is_global and not ip.is_multicast


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def check_public_url(url: str) -> tuple[str, int]:
    """Syntax and name only (no DNS): returns (host in lower case without a trailing dot, port, 443 by default)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise NotPublic(PRIVATE_ADDRESS) from None
    if parts.scheme != "https":
        raise NotPublic(NOT_HTTPS)
    host = (host or "").rstrip(".")
    if not host:
        raise NotPublic(PRIVATE_ADDRESS)
    if _ip_literal(host):
        if not is_public_ip(host):
            raise NotPublic(PRIVATE_ADDRESS)
    elif "." not in host or host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES):
        raise NotPublic(LOCAL_NAME)
    return host, 443 if port is None else port


def _all_public(addresses: list[str]) -> bool:
    try:
        return all(is_public_ip(a) for a in addresses)
    except ValueError:
        return False


async def resolve_public(host: str, port: int, resolver: Resolver, *, timeout_s: float = 5.0) -> list[str]:
    """The host's addresses, all of them public; an IP literal comes back as is. Raises NotPublic."""
    host = host.removeprefix("[").removesuffix("]")
    if _ip_literal(host):
        if not is_public_ip(host):
            raise NotPublic(PRIVATE_ADDRESS)
        return [host]
    try:
        async with asyncio.timeout(timeout_s):
            addresses = list(await resolver(host, port))
    except (OSError, TimeoutError, ValueError):
        raise NotPublic(NO_ADDRESS) from None
    if not addresses:
        raise NotPublic(NO_ADDRESS)
    if not _all_public(addresses):
        raise NotPublic(RESOLVES_PRIVATE)
    return addresses


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself, checks every address, and connects to a checked one (each in order)."""

    def __init__(self, resolver: Resolver, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._resolver = resolver
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await resolve_public(host, port, self._resolver)
        except NotPublic as e:
            raise UrlNotPublic(str(e)) from None
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
        assert last is not None  # resolve_public never returns an empty list
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UrlNotPublic(LOCAL_NAME)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PublicOnlyTransport(httpx.AsyncHTTPTransport):
    # Swaps httpx's private pool for one on the checking backend, with httpx's default limits.
    # test_netguard's client tests catch an httpx update that renames or stops using `_pool`.
    def __init__(self, resolver: Resolver) -> None:
        super().__init__()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=PublicOnlyBackend(resolver),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
        )


def public_http_client(resolver: Resolver, *, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """The client for users' own endpoints. An explicit transport keeps proxies from the environment out (httpx mounts
    them only without one), trust_env=False keeps .netrc out, and redirects are not followed."""
    return httpx.AsyncClient(
        transport=_PublicOnlyTransport(resolver), timeout=timeout, trust_env=False, follow_redirects=False
    )
