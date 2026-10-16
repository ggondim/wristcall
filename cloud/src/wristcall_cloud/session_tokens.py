"""Opaque secrets, ids and the account access token (`wc-account+jwt`, ES256).

The access token is signed by the same key as the per-server tokens, told apart by the header `typ`, so a
per-server token (meant for a server) is never taken as a credential for the Cloud and vice versa. Secrets
(account key, refresh tokens, codes) are stored only as SHA-256 hashes; nothing here puts one in a message.
"""

import base64
import binascii
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

from .signing import ALGORITHM, SigningKey

ACCOUNT_TOKEN_TYPE = "wc-account+jwt"
PLATFORMS = ("ios", "ipados", "macos", "watchos", "web")
APP_PLATFORMS = frozenset({"ios", "ipados", "macos"})  # holders of the account key (iCloud Keychain)
MANAGER_PLATFORMS = APP_PLATFORMS | {"web"}
MAX_TOKEN_LENGTH = 4096
_KEY_LENGTH = 43  # 32 bytes in base64url without padding


class TokenError(Exception):
    """The account token is not acceptable. Never carries the token."""


def new_secret(prefix: str, nbytes: int = 32) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def new_code(prefix: str) -> str:
    """128 random bits as 26 lowercase base32 characters."""
    return prefix + base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_account_id() -> str:
    return new_code("acc_")


def new_device_id() -> str:
    return "dev_" + secrets.token_hex(8)


def parse_account_key(value: Any) -> str:
    """The SHA-256 (hex) of an account key: 32 bytes as base64url without padding, in its canonical form."""
    error = ValueError("account_key must be 32 bytes in base64url")
    if not isinstance(value, str) or len(value) != _KEY_LENGTH or not value.isascii():
        raise error
    try:
        raw = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise error from None
    # Canonical form only: the 2 spare bits of the last character must be zero, or one key would have two spellings.
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        raise error
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class AccountClaims:
    account: str
    device: str
    platform: str
    expires_at: float


class AccountTokens:
    def __init__(
        self,
        key: SigningKey,
        issuer: str,
        *,
        ttl_s: int = 3600,
        now: Callable[[], float] = time.time,
        leeway_s: float = 60,
    ) -> None:
        self._key = key
        self._issuer = issuer
        self._ttl = ttl_s
        self._now = now
        self._leeway = leeway_s

    def issue(self, account: str, device: str, platform: str) -> tuple[str, int]:
        issued = int(self._now())
        expires = issued + self._ttl
        claims = {
            "iss": self._issuer,
            "aud": self._issuer,
            "sub": account,
            "did": device,
            "plt": platform,
            "iat": issued,
            "exp": expires,
            "jti": secrets.token_hex(16),
        }
        return self._key.sign(claims, typ=ACCOUNT_TOKEN_TYPE), expires

    def verify(self, token: str) -> AccountClaims:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
            raise TokenError("invalid account token")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("typ") != ACCOUNT_TOKEN_TYPE or header.get("alg") != ALGORITHM:
                raise TokenError("invalid account token")
            claims = jwt.decode(
                token,
                self._key.public_key,
                algorithms=[ALGORITHM],
                audience=self._issuer,
                issuer=self._issuer,
                # Time claims are checked below against the injected clock, not the wall clock.
                options={"require": ["exp", "iat", "iss", "aud", "sub"], "verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            raise TokenError("invalid account token") from None
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or not isinstance(iat, (int, float)):
            raise TokenError("invalid account token")
        now = self._now()
        if exp + self._leeway < now:
            raise TokenError("account token expired")
        if iat - self._leeway > now:
            raise TokenError("invalid account token")
        account, device, platform = claims.get("sub"), claims.get("did"), claims.get("plt")
        if not all(isinstance(v, str) and v for v in (account, device)) or platform not in PLATFORMS:
            raise TokenError("invalid account token")
        return AccountClaims(account=account, device=device, platform=platform, expires_at=float(exp))

    @staticmethod
    def looks_like(token: str) -> bool:
        """Whether the header says this is an account token (nothing is verified): routes the credential."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        try:
            return jwt.get_unverified_header(token).get("typ") == ACCOUNT_TOKEN_TYPE
        except jwt.PyJWTError:
            return False
