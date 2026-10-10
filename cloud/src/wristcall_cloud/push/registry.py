"""Push keys, registrations and messages: what a device may register and what a server may send.

A push key is `wc_push_` + 43 base64url characters. The Cloud stores only its SHA-256 (the registration's `_id`), so a
copy of the database cannot send anything. The label and the tag of a registration are forced on every message sent
with its key: a leaked key cannot pass for another server.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from pymongo.errors import PyMongoError

from ..config import CloudConfig
from .channels import Message

log = logging.getLogger("wristcall_cloud.push")

PUSH_KEY_PREFIX = "wc_push_"
EVENTS = frozenset({"call.finished", "device.approval"})  # "test" is always accepted and is not in the list
PLATFORMS = ("apns", "webpush")
ENVIRONMENTS = ("sandbox", "production")

MAX_LABEL = 64
MAX_TAG = 64
MAX_TITLE = 100
MAX_BODY = 300
MAX_DATA = 1024  # bytes of compact JSON
MAX_TTL = 86400
DEFAULT_TTL = 3600
MAX_ENDPOINT = 2048

_PUSH_KEY = re.compile(re.escape(PUSH_KEY_PREFIX) + r"[A-Za-z0-9_-]{43}")
_TAG = re.compile(r"[A-Za-z0-9_.:-]*")
_COLLAPSE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_APNS_TOKEN = re.compile(r"(?:[0-9a-fA-F]{2}){32,100}")
_B64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")

_COMMON_FIELDS = {"platform", "label", "tag", "events"}
_APNS_FIELDS = _COMMON_FIELDS | {"token", "topic", "environment"}
_WEBPUSH_FIELDS = _COMMON_FIELDS | {"subscription"}
_SUBSCRIPTION_FIELDS = {"endpoint", "keys", "expirationTime"}  # what PushSubscription.toJSON() gives
_MESSAGE_FIELDS = {"event", "title", "body", "data", "ttl_s", "collapse_id"}


class PushError(Exception):
    """`code` is the API error code; the message never carries a key, a token or an endpoint."""

    def __init__(self, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


def new_push_key() -> str:
    return PUSH_KEY_PREFIX + secrets.token_urlsafe(32)


def key_id(push_key: str) -> str:
    """The registration's `_id`: the key itself is never stored."""
    return hashlib.sha256(push_key.encode()).hexdigest()


def is_push_key(value: str) -> bool:
    return _PUSH_KEY.fullmatch(value) is not None


def _invalid(message: str) -> PushError:
    return PushError("invalid", message)


def _no_extra(body: dict[str, Any], allowed: set[str], what: str) -> None:
    if set(body) - allowed:
        raise _invalid(f"{what}: fields not allowed")


def _text(value: Any, field: str, low: int, high: int, *, newlines: bool = False) -> str:
    """A string of `low`..`high` characters without control characters (newlines allowed when asked) or lone
    surrogates (valid JSON escapes, but not text: they cannot be encoded)."""
    if not isinstance(value, str):
        raise _invalid(f"{field} must be a string")
    if not low <= len(value) <= high:
        raise _invalid(f"{field} must be {low} to {high} characters")
    if any(unicodedata.category(c) in ("Cc", "Cs") and not (newlines and c == "\n") for c in value):
        raise _invalid(f"{field} must not contain control characters")
    return value


def _b64url(value: Any, field: str, size: int) -> str:
    """Base64url (padded or not) of exactly `size` bytes, given back without padding."""
    if not isinstance(value, str) or not _B64URL.fullmatch(value):
        raise _invalid(f"{field} must be base64url")
    bare = value.rstrip("=")
    try:
        raw = base64.urlsafe_b64decode(bare + "=" * (-len(bare) % 4))
    except (binascii.Error, ValueError):
        raise _invalid(f"{field} must be base64url") from None
    if len(raw) != size:
        raise _invalid(f"{field} must be {size} bytes")
    return bare


def _endpoint(value: Any) -> str:
    """An https URL. The endpoint is a secret: it never reaches an error message."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_ENDPOINT:
        raise _invalid(f"subscription.endpoint must be a URL of 1 to {MAX_ENDPOINT} characters")
    if not value.isascii() or any(c.isspace() or not c.isprintable() or c == "\\" for c in value):
        raise _invalid("subscription.endpoint must be an https URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise _invalid("subscription.endpoint must be an https URL") from None
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise _invalid("subscription.endpoint must be an https URL")
    return value


def _events(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _invalid("events must be a non-empty list")
    if not all(isinstance(e, str) and e in EVENTS for e in value):
        raise _invalid("events must be among " + ", ".join(sorted(EVENTS)))
    if len(set(value)) != len(value):
        raise _invalid("events must not repeat")
    return list(value)


def parse_registration(body: Any, config: CloudConfig) -> dict[str, Any]:
    """The registration document without `_id` and times: `platform`, `channel` (APNs token or Web Push endpoint),
    the platform's fields, `label`, `tag` and `events`."""
    if not isinstance(body, dict):
        raise _invalid("body must be a JSON object")
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise _invalid("platform must be one of " + ", ".join(PLATFORMS))
    tag = body.get("tag", "")
    if not isinstance(tag, str) or len(tag) > MAX_TAG or not _TAG.fullmatch(tag):
        raise _invalid(f"tag must be up to {MAX_TAG} characters of A-Z a-z 0-9 _ . : -")
    label = body.get("label")
    common = {
        "label": _text(label.strip() if isinstance(label, str) else label, "label", 1, MAX_LABEL),
        "tag": tag,
        "events": _events(body.get("events")),
    }
    if platform == "apns":
        _no_extra(body, _APNS_FIELDS, "registration")
        token = body.get("token")
        if not isinstance(token, str) or not _APNS_TOKEN.fullmatch(token):
            raise _invalid("token must be an APNs device token (hex)")
        if body.get("topic") not in config.apns_topics:
            raise _invalid("topic is not an app of this cloud")
        if body.get("environment") not in ENVIRONMENTS:
            raise _invalid("environment must be one of " + ", ".join(ENVIRONMENTS))
        return {"platform": "apns", "channel": token.lower(), "topic": body["topic"],
                "environment": body["environment"], **common}
    _no_extra(body, _WEBPUSH_FIELDS, "registration")
    subscription = body.get("subscription")
    if not isinstance(subscription, dict):
        raise _invalid("subscription must be an object")
    _no_extra(subscription, _SUBSCRIPTION_FIELDS, "subscription")
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        raise _invalid("subscription.keys must be an object")
    _no_extra(keys, {"p256dh", "auth"}, "subscription.keys")
    p256dh = _b64url(keys.get("p256dh"), "subscription.keys.p256dh", 65)
    if base64.urlsafe_b64decode(p256dh + "=" * (-len(p256dh) % 4))[0] != 4:
        raise _invalid("subscription.keys.p256dh must be an uncompressed P-256 point")
    return {
        "platform": "webpush",
        "channel": _endpoint(subscription.get("endpoint")),
        "p256dh": p256dh,
        "auth": _b64url(keys.get("auth"), "subscription.keys.auth", 16),
        **common,
    }


def public_registration(doc: dict[str, Any]) -> dict[str, Any]:
    """A registration as the API shows it: never the channel."""
    return {k: doc[k] for k in ("platform", "label", "tag", "events")}


def parse_message(body: Any, registration: dict[str, Any]) -> Message:
    """What a server sends; subtitle and tag come from the registration, never from the sender."""
    if not isinstance(body, dict):
        raise _invalid("body must be a JSON object")
    _no_extra(body, _MESSAGE_FIELDS, "message")
    event = body.get("event")
    if not isinstance(event, str) or (event != "test" and event not in registration["events"]):
        raise _invalid("event is not one this registration accepts")
    data = body.get("data", {})
    if not isinstance(data, dict):
        raise _invalid("data must be an object")
    try:
        size = len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())
    except UnicodeEncodeError:
        raise _invalid("data must be valid text") from None
    if size > MAX_DATA:
        raise _invalid(f"data must be up to {MAX_DATA} bytes")
    ttl_s = body.get("ttl_s", DEFAULT_TTL)
    if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or not 0 <= ttl_s <= MAX_TTL:
        raise _invalid(f"ttl_s must be an integer from 0 to {MAX_TTL}")
    collapse_id = body.get("collapse_id")
    if collapse_id is not None and (not isinstance(collapse_id, str) or not _COLLAPSE_ID.fullmatch(collapse_id)):
        raise _invalid("collapse_id must be 1 to 64 characters of A-Z a-z 0-9 _ . : -")
    return Message(
        event=event,
        title=_text(body.get("title"), "title", 1, MAX_TITLE),
        body=_text(body.get("body", ""), "body", 0, MAX_BODY, newlines=True),
        subtitle=registration["label"],
        data=data,
        ttl_s=ttl_s,
        collapse_id=collapse_id,
        tag=registration["tag"],
    )


class _Purger(Protocol):
    async def purge_idle_registrations(self, before: float) -> int: ...


async def purge_idle_loop(
    store: _Purger,
    *,
    every_s: float,
    idle_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.time,
) -> None:
    """Every `every_s`, deletes the registrations without a send for `idle_s`. Runs until cancelled; a failed round
    is logged and the next one tries again."""
    while True:
        await sleep(every_s)
        try:
            purged = await store.purge_idle_registrations(now() - idle_s)
        except PyMongoError as e:
            log.warning("purging idle push registrations failed (%s)", type(e).__name__)
            continue
        if purged:
            log.info("purged %d idle push registrations", purged)
