"""Anti-abuse limits of the account routes, in memory like the push relay's (push/limits.py): one replica, lost on
restart. Per-IP limits count IPv4 addresses whole and IPv6 addresses by their /64, which one client usually holds."""

import ipaddress
import math
import time
from collections.abc import Callable

from fastapi import Request

from .api_common import ApiError, client_ip
from .config import CloudConfig
from .push.limits import _Limiter, _Window


class AccountCreationLimiter(_Limiter):
    """Silent accounts per client IP: `per_hour` and `per_day`."""

    def __init__(self, per_hour: int, per_day: int, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__([_Window(3600, per_hour), _Window(86400, per_day)], now)


class AttemptLimiter(_Limiter):
    """Attempts per client IP on the routes that take a guessable secret (account key, code, web session)."""

    def __init__(self, per_minute: int, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__([_Window(60, per_minute)], now)


class PollLimiter(_Limiter):
    """Polls per web session: one a second."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__([_Window(1, 1)], now)


def limit_key(ip: str) -> str:
    """The key an address is limited by: an IPv4 address whole (an IPv4-mapped IPv6 one too), an IPv6 address by
    its /64 ("2001:db8:1:2::/64"), anything that is not an address as it is."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(address), 64), strict=False))
    return str(address)


def check_ip_limit(request: Request, limiter: _Limiter) -> None:
    """Counts the request against the client's address; 429 `rate_limited` (with Retry-After) when exhausted."""
    config: CloudConfig = request.app.state.config
    wait = limiter.allow(limit_key(client_ip(request, config.client_ip_header)))
    if wait is not None:
        raise ApiError("rate_limited", "too many requests; try again later", 429, retry_after=math.ceil(wait))
