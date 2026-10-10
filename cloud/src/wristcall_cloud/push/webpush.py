"""Web Push channel: message encryption (RFC 8291, aes128gcm of RFC 8188) and VAPID (RFC 8292), done here with
`cryptography` and PyJWT on an async httpx client.

The endpoint is a secret URL the browser's push service made up: it never reaches a log or an error. The Cloud only
posts to the push services on its list (`endpoint_allowed`), checked again before every send, and never follows a
redirect: a registration cannot make the Cloud call anything else.
"""

import base64
import hashlib
import json
import logging
import os
import re
import struct
import time
from collections.abc import Sequence
from typing import Any

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .channels import ChannelGone, ChannelUnavailable, Message

log = logging.getLogger("wristcall_cloud.push")

# Apple, Google (Chrome, Edge on Android), Mozilla and Microsoft. A leading dot: any subdomain; otherwise that host.
DEFAULT_HOSTS = (".push.apple.com", "fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com")
VAPID_TTL_S = 12 * 3600
VAPID_RENEW_S = 3600  # a cached VAPID header is replaced this long before it expires
VAPID_CACHE_MAX = 1000  # origins; past it the cache starts over (there are a handful of push services)
OK = frozenset({200, 201, 202})
GONE = frozenset({404, 410})
REFUSED = frozenset({400, 403, 413})  # our request was wrong (logged with the status); trying later may not help
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SUBJECTS = ("mailto:", "https://")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _point(public_key: ec.EllipticCurvePublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def endpoint_allowed(endpoint: str, hosts: Sequence[str]) -> bool:
    """Whether the Cloud may post to `endpoint`: https on the default port, no user info, and a host on the list.
    Parsed by httpx, the parser that sends, so what is checked is what is called."""
    if not isinstance(endpoint, str) or any(c.isspace() or not c.isprintable() or c == "\\" for c in endpoint):
        return False
    try:
        url = httpx.URL(endpoint)
        host = url.raw_host.decode("ascii").lower()
    except (httpx.InvalidURL, ValueError):
        return False
    if url.scheme != "https" or url.userinfo or url.port not in (None, 443) or not host:
        return False
    return any(host.endswith(h.lower()) if h.startswith(".") else host == h.lower() for h in hosts)


def _hmac(key: bytes, data: bytes) -> bytes:
    mac = hmac.HMAC(key, hashes.SHA256())
    mac.update(data)
    return mac.finalize()


def encrypt(
    plaintext: bytes,
    p256dh: bytes,
    auth: bytes,
    *,
    salt: bytes | None = None,
    private_key: ec.EllipticCurvePrivateKey | None = None,
    record_size: int = 4096,
) -> bytes:
    """The body of a Web Push message for the browser key `p256dh` (65-byte uncompressed P-256 point) and its
    `auth` secret: one aes128gcm record ending with the 0x02 delimiter (RFC 8291 §3.4, RFC 8188). `salt` and
    `private_key` (the one-time key of this message) are only given by tests. Raises ValueError on a key that is
    not a point of the curve or a plaintext that does not fit one record."""
    if len(p256dh) != 65 or len(auth) != 16:
        raise ValueError("p256dh must be 65 bytes and auth 16 bytes")
    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    record = plaintext + b"\x02"
    if len(record) + 16 > record_size:
        raise ValueError("the message does not fit one record")
    as_private = private_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(as_private.public_key())
    salt = os.urandom(16) if salt is None else salt
    if len(salt) != 16:
        raise ValueError("salt must be 16 bytes")
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_public)
    prk_key = _hmac(auth, ecdh_secret)
    key_info = b"WebPush: info\x00" + p256dh + as_public
    ikm = _hmac(prk_key, key_info + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    header = salt + struct.pack("!IB", record_size, len(as_public)) + as_public
    return header + AESGCM(cek).encrypt(nonce, record, None)


class Vapid:
    """The Cloud's VAPID identity: a P-256 key whose public half browsers subscribe with (`public_key`, given to
    the apps by /v1/config), and a contact (`mailto:` or `https:`) push services may write to."""

    def __init__(self, private_pem: bytes, subject: str) -> None:
        if not isinstance(subject, str) or not subject.startswith(_SUBJECTS) or subject in _SUBJECTS:
            raise ValueError("VAPID subject must be a mailto: or https:// URL")
        try:
            key = serialization.load_pem_private_key(private_pem, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            key = None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("VAPID key must be an EC P-256 private key")
        self._key = key
        self.subject = subject
        self.public_key: str = _b64(_point(key.public_key()))
        self._cache: dict[str, tuple[str, int]] = {}  # origin -> (header, exp)

    def __repr__(self) -> str:
        return f"Vapid(public_key={self.public_key!r})"

    def header(self, endpoint: str, now: float) -> str:
        """`Authorization` for a push to `endpoint`: a JWT for its origin, valid 12 h, reused until 1 h before."""
        url = httpx.URL(endpoint)
        origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
        cached = self._cache.get(origin)
        if cached is not None and now < cached[1] - VAPID_RENEW_S:
            return cached[0]
        exp = int(now) + VAPID_TTL_S
        token = jwt.encode({"aud": origin, "exp": exp, "sub": self.subject}, self._key, algorithm="ES256")
        value = f"vapid t={token}, k={self.public_key}"
        self._cache = {o: c for o, c in self._cache.items() if now < c[1] - VAPID_RENEW_S}
        if len(self._cache) >= VAPID_CACHE_MAX:
            self._cache = {}
        self._cache[origin] = (value, exp)
        return value


def _topic(collapse_id: str | None) -> str | None:
    """The `Topic` header (RFC 8030: up to 32 base64url characters). A collapse id that is not one as is becomes a
    digest of it: the same id always gives the same topic, so a message still replaces the previous one."""
    if collapse_id is None:
        return None
    if _TOPIC.fullmatch(collapse_id):
        return collapse_id
    return _b64(hashlib.sha256(collapse_id.encode()).digest())[:32]


class WebPushChannel:
    """Delivers a message to a Web Push subscription. Never logs the endpoint (it is a secret)."""

    def __init__(
        self, vapid: Vapid, http: httpx.AsyncClient, hosts: Sequence[str] = DEFAULT_HOSTS, timeout_s: float = 10
    ) -> None:
        self.vapid = vapid
        self.http = http
        self.hosts = tuple(hosts)
        self.timeout_s = timeout_s

    async def send(self, registration: dict[str, Any], message: Message) -> None:
        endpoint: str = registration["channel"]
        # The list may have changed since the registration was made.
        if not endpoint_allowed(endpoint, self.hosts):
            log.warning("web push endpoint is no longer on the list of push services")
            raise ChannelGone("endpoint not allowed")
        p256dh, auth = _unb64(registration["p256dh"]), _unb64(registration["auth"])
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
        except ValueError:
            log.warning("web push subscription key is not a P-256 point")
            raise ChannelGone("invalid subscription key") from None
        payload = {
            "v": 1,
            "event": message.event,
            "tag": message.tag,
            "title": message.title,
            "subtitle": message.subtitle,
            "body": message.body,
            "data": message.data,
        }
        plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        try:
            body = encrypt(plaintext, p256dh, auth)
        except ValueError as e:  # our own messages, no secret in them
            log.warning("web push message not encrypted (%s)", e)
            raise ChannelUnavailable("message not encrypted") from None
        headers = {
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(message.ttl_s),
            "Urgency": "high",
            "Authorization": self.vapid.header(endpoint, time.time()),
        }
        topic = _topic(message.collapse_id)
        if topic is not None:
            headers["Topic"] = topic
        try:
            response = await self.http.post(
                endpoint, content=body, headers=headers, timeout=self.timeout_s, follow_redirects=False
            )
        except httpx.HTTPError as e:  # its message may carry the URL: only its type is logged
            log.warning("web push service unreachable (%s)", type(e).__name__)
            raise ChannelUnavailable("push service unreachable") from None
        status = response.status_code
        if status in OK:
            return
        if status in GONE:
            log.info("web push subscription gone (status %d)", status)
            raise ChannelGone("subscription gone")
        if status in REFUSED:
            log.warning("web push service refused the message (status %d)", status)
        else:
            log.warning("web push service unavailable (status %d)", status)
        raise ChannelUnavailable(f"push service answered {status}")
