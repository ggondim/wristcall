"""Webhook: the action of one-shot and monologue agents. One POST of the call's transcript per attempt."""

import asyncio
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .. import __version__
from . import ProviderError, register

_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
# Set by the server on every request; the agent's headers cannot replace them.
_RESERVED = {
    "content-type", "content-length", "host", "user-agent", "idempotency-key", "transfer-encoding", "connection", "expect",
}
MAX_HEADERS = 16


class WebhookError(ProviderError):
    """An attempt that got no HTTP answer: "timeout" or "connection" (refused, DNS, TLS...)."""

    def __init__(self, reason: Literal["timeout", "connection"]) -> None:
        super().__init__(f"webhook {reason}")
        self.reason = reason


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("url must not hold credentials, a query or a fragment; put secrets in headers")
    return url


def check_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
        raise ValueError(f"headers must be an object with up to {MAX_HEADERS} entries")
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name) or name.lower() in _RESERVED:
            raise ValueError("headers: invalid or reserved header name")
        # HTTP sends header values as ASCII: anything else would fail at every delivery, not here.
        if not isinstance(value, str) or len(value) > 4096 or not value.isascii() or not value.isprintable():
            raise ValueError("headers: values must be printable ASCII on one line")
    return dict(headers)


@register("webhook", "webhook")
class Webhook:
    def __init__(self, options: dict[str, Any], http: httpx.AsyncClient) -> None:
        self._http = http
        self._url = check_url(str(options["url"]))
        self._headers = check_headers(options.get("headers"))

    async def send(self, body: dict[str, Any], *, idempotency_key: str, timeout_s: float) -> int:
        """One attempt; returns the HTTP status. Redirects are not followed (a 3xx is an answer, not a 2xx)."""
        headers = {
            **self._headers,
            "User-Agent": f"wristcall/{__version__}",
            "Idempotency-Key": idempotency_key,
        }
        try:
            # httpx timeouts are per phase; the deadline bounds the whole attempt. The body is never read.
            async with asyncio.timeout(timeout_s):
                async with self._http.stream(
                    "POST", self._url, json=body, headers=headers, timeout=timeout_s, follow_redirects=False
                ) as r:
                    return r.status_code
        except (TimeoutError, httpx.TimeoutException):
            raise WebhookError("timeout") from None
        except httpx.HTTPError:
            raise WebhookError("connection") from None
