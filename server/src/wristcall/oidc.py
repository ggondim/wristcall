"""Validates tokens issued by the central account (OIDC, decision 17) against the issuer's JWKS.

The server never calls the issuer's APIs with the token: it checks the signature with the published keys,
the issuer, the audience and the time claims. Keys are cached; an unknown key id triggers at most one
refetch per `refetch_min_s` so a stream of forged tokens cannot turn the server into a request amplifier.
"""

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from . import __version__

ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA"})
MAX_TOKEN_LENGTH = 8192
REQUIRED_CLAIMS = ["exp", "iat", "iss", "sub", "aud"]


class OidcError(Exception):
    """The token is not acceptable (bad format, signature, issuer, audience or time). Never carries the token."""


class OidcUnavailable(Exception):
    """The issuer's discovery document or keys could not be fetched and nothing usable is cached."""


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    client_id: str | None
    expires_at: float


def normalize_issuer(issuer: str) -> str:
    return issuer.rstrip("/")


class OidcVerifier:
    def __init__(
        self,
        issuer: str,
        audiences: Sequence[str],
        http: httpx.AsyncClient,
        *,
        now: Callable[[], float] = time.time,
        jwks_ttl_s: float = 3600,
        refetch_min_s: float = 60,
        leeway_s: float = 60,
        clients: Sequence[str] | None = None,
        typ: str | None = None,
    ) -> None:
        if not audiences:
            raise ValueError("at least one audience is required")
        self.issuer = normalize_issuer(issuer)
        self._audiences = list(audiences)
        # With clients set, the token must also have been issued to one of them (client_id or azp claim).
        self._clients = set(clients) if clients is not None else None
        # When set, the JWT header's `typ` must be exactly this (a per-server token, not any token of the issuer).
        self._typ = typ
        self._http = http
        self._now = now
        self._ttl = jwks_ttl_s
        self._refetch_min = refetch_min_s
        self._leeway = leeway_s
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> Identity:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
            raise OidcError("malformed token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise OidcError("malformed token") from None
        if self._typ is not None and header.get("typ") != self._typ:
            raise OidcError("unexpected token type")
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALGORITHMS:
            raise OidcError("unsupported signing algorithm")
        if not isinstance(kid, str) or not kid:
            raise OidcError("token has no key id")
        key = await self._key(kid)
        if key is None:
            raise OidcError("unknown signing key")
        # The key must be meant for this algorithm: an RSA key cannot be reused with a different family.
        if key.algorithm_name != alg:
            raise OidcError("signing algorithm does not match the key")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[alg],
                audience=self._audiences,
                issuer=self.issuer,
                leeway=self._leeway,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError:
            raise OidcError("token expired") from None
        except jwt.ImmatureSignatureError:
            raise OidcError("token not valid yet; check the server clock") from None
        except jwt.InvalidAudienceError:
            raise OidcError("token is not meant for this server") from None
        except jwt.InvalidIssuerError:
            raise OidcError("token comes from another issuer") from None
        except jwt.PyJWTError:
            raise OidcError("invalid token") from None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise OidcError("invalid subject")
        # ID tokens are signed by the same keys; only access tokens are credentials for this server.
        if "nonce" in claims or "at_hash" in claims:
            raise OidcError("ID tokens are not accepted; send an access token")
        client_id = claims.get("client_id", claims.get("azp"))
        if self._clients is not None and client_id not in self._clients:
            raise OidcError("token was issued to another client")
        return Identity(
            issuer=self.issuer,
            subject=subject,
            client_id=client_id if isinstance(client_id, str) else None,
            expires_at=float(claims["exp"]),
        )

    def _fresh(self) -> bool:
        return self._fetched_at is not None and self._now() - self._fetched_at < self._ttl

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        if self._fresh() and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            # Another request may have refreshed the keys while this one waited.
            if self._fresh() and kid in self._keys:
                return self._keys[kid]
            # One attempt per refetch_min_s, successful or not: neither forged key ids nor an issuer outage
            # turn into a stream of requests (or a queue of 10 s timeouts) against the issuer.
            if self._attempted_at is None or self._now() - self._attempted_at >= self._refetch_min:
                self._attempted_at = self._now()
                try:
                    await self._refresh()
                except OidcUnavailable:
                    # Stale keys beat no keys: the issuer being down must not lock every device out.
                    if kid in self._keys:
                        return self._keys[kid]
                    raise
            elif not self._keys:
                raise OidcUnavailable("issuer unavailable; retrying later")
        return self._keys.get(kid)

    async def _refresh(self) -> None:
        discovery = await self._get_json(f"{self.issuer}/.well-known/openid-configuration")
        if normalize_issuer(str(discovery.get("issuer", ""))) != self.issuer:
            raise OidcUnavailable("discovery document names another issuer")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str) or httpx.URL(jwks_uri).scheme != httpx.URL(self.issuer).scheme:
            raise OidcUnavailable("discovery document has no usable jwks_uri")
        jwks = await self._get_json(jwks_uri)
        keys: dict[str, jwt.PyJWK] = {}
        for entry in jwks.get("keys", []) if isinstance(jwks.get("keys"), list) else []:
            if not isinstance(entry, dict) or entry.get("use", "sig") != "sig" or not entry.get("kid"):
                continue
            try:
                keys[str(entry["kid"])] = jwt.PyJWK(entry)
            except jwt.PyJWTError:
                continue
        if not keys:
            raise OidcUnavailable("issuer published no usable signing keys")
        self._keys = keys
        self._fetched_at = self._now()

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            r = await self._http.get(
                url, headers={"User-Agent": f"wristcall-server/{__version__}", "Accept": "application/json"}, timeout=10.0
            )
        except httpx.HTTPError as e:
            raise OidcUnavailable(f"issuer unreachable: {type(e).__name__}") from None
        if r.status_code != 200:
            raise OidcUnavailable(f"issuer responded {r.status_code}")
        try:
            body = r.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OidcUnavailable("issuer sent invalid JSON") from None
        if not isinstance(body, dict):
            raise OidcUnavailable("issuer sent invalid JSON")
        return body
