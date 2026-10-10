"""APNs channel: HTTP/2 requests authenticated with a provider token (a JWT signed with the team's `.p8` key, ES256),
on an async httpx client.

The device token is a secret: it never reaches a log or an error (only statuses and Apple's reason codes do).
"""

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .channels import ChannelGone, ChannelUnavailable, Message

log = logging.getLogger("wristcall_cloud.push")

APNS_HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
CATEGORIES = {"call.finished": "WC_CALL_FINISHED", "device.approval": "WC_DEVICE_APPROVAL", "test": "WC_TEST"}
MAX_PAYLOAD = 4096  # bytes, what APNs takes for an alert
GONE_REASONS = frozenset({"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"})
TOKEN_REASONS = frozenset({"ExpiredProviderToken", "InvalidProviderToken"})
_REASON = re.compile(r"[A-Za-z]{1,64}")


class ProviderToken:
    """The provider token APNs authenticates the Cloud with. Apple refuses one older than an hour and one renewed
    more often than every 20 minutes, so the same token is reused for `refresh_s`."""

    def __init__(
        self, key_pem: bytes, key_id: str, team_id: str, *, now: Callable[[], float] = time.time,
        refresh_s: float = 1800,
    ) -> None:
        try:
            key = serialization.load_pem_private_key(key_pem, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            key = None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("APNs key must be an EC P-256 private key")
        self._key = key
        self.key_id = key_id
        self.team_id = team_id
        self._now = now
        self.refresh_s = refresh_s
        self._token: str | None = None
        self._issued_at = 0.0

    def __repr__(self) -> str:
        return f"ProviderToken(key_id={self.key_id!r}, team_id={self.team_id!r})"

    def get(self) -> str:
        now = self._now()
        if self._token is None or now - self._issued_at >= self.refresh_s:
            self._token = jwt.encode(
                {"iss": self.team_id, "iat": int(now)}, self._key, algorithm="ES256",
                headers={"kid": self.key_id, "typ": None},
            )
            self._issued_at = now
        return self._token

    def invalidate(self, token: str) -> None:
        """Drops `token` if it is still the current one: sends that failed with it at the same time renew it once,
        not once each (APNs refuses renewals too close together: TooManyProviderTokenUpdates)."""
        if token == self._token:
            self._token = None


def payload(message: Message) -> bytes:
    body = {
        "aps": {
            "alert": {"title": message.title, "subtitle": message.subtitle, "body": message.body},
            "sound": "default",
            "thread-id": message.event,
            "category": CATEGORIES[message.event],
        },
        "wristcall": {"v": 1, "event": message.event, "tag": message.tag, "data": message.data},
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _reason(response: httpx.Response) -> str:
    """Apple's reason code (`{"reason": "BadDeviceToken"}`), or "unknown" for anything that is not one."""
    try:
        value = response.json().get("reason")
    except (ValueError, AttributeError):
        return "unknown"
    return value if isinstance(value, str) and _REASON.fullmatch(value) else "unknown"


class ApnsChannel:
    """Delivers a message to an APNs device token. Never logs the device token (it is in the request path)."""

    def __init__(
        self,
        token: ProviderToken,
        http: httpx.AsyncClient,
        *,
        hosts: Mapping[str, str] = APNS_HOSTS,
        timeout_s: float = 10,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.token = token
        self.http = http
        self.hosts = dict(hosts)
        self.timeout_s = timeout_s
        self._now = now

    async def send(self, registration: dict[str, Any], message: Message) -> None:
        host = self.hosts.get(registration["environment"])
        if host is None:
            log.warning("apns environment not configured (%s)", registration["environment"])
            raise ChannelUnavailable("environment not configured")
        content = payload(message)
        if len(content) > MAX_PAYLOAD:
            log.warning("apns payload too large (%d bytes)", len(content))
            raise ChannelUnavailable("payload too large")
        url = f"{host}/3/device/{registration['channel']}"
        headers = {
            "apns-topic": registration["topic"],
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(int(self._now() + message.ttl_s)) if message.ttl_s else "0",
            "content-type": "application/json",
        }
        if message.collapse_id is not None:
            headers["apns-collapse-id"] = message.collapse_id
        for attempt in (1, 2):
            jwt_token = self.token.get()
            try:
                response = await self.http.post(
                    url, content=content, headers={**headers, "authorization": f"bearer {jwt_token}"},
                    timeout=self.timeout_s, follow_redirects=False,
                )
            except httpx.HTTPError as e:  # its message may carry the URL: only its type is logged
                log.warning("apns unreachable (%s)", type(e).__name__)
                raise ChannelUnavailable("apns unreachable") from None
            status = response.status_code
            if status == 200:
                return
            reason = _reason(response)
            if status == 410 or (status == 400 and reason in GONE_REASONS):
                log.info("apns device token gone (status %d, reason %s)", status, reason)
                raise ChannelGone("device token gone")
            if status == 403 and reason in TOKEN_REASONS and attempt == 1:
                # Only the first refusal drops the token: one refused again on the retry is kept, or every send
                # would ask for a new one (APNs refuses renewals too close together).
                self.token.invalidate(jwt_token)
                log.info("apns provider token refused (reason %s), trying a new one", reason)
                continue
            log.warning("apns unavailable (status %d, reason %s)", status, reason)
            raise ChannelUnavailable(f"apns answered {status}")
        raise AssertionError("unreachable")  # pragma: no cover
