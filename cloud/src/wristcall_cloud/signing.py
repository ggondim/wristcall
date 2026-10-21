"""The key the Cloud signs per-server tokens with (ES256), published at /v1/jwks.

A per-server token carries the central account (`sub`) to one server only (`aud` = the URL the app connects to):
a server that receives one cannot replay it against another server or against the Cloud.
"""

import base64
import hashlib
import secrets
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

ALGORITHM = "ES256"
TOKEN_TYPE = "wc-server+jwt"


class SigningKey:
    def __init__(self, private_pem: bytes) -> None:
        try:
            key = serialization.load_pem_private_key(private_pem, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            key = None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("signing key must be an EC P-256 private key")
        self._key = key
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.kid: str = base64.urlsafe_b64encode(hashlib.sha256(public_der).digest()).decode().rstrip("=")[:16]

    def __repr__(self) -> str:
        return f"SigningKey(kid={self.kid!r})"

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._key.public_key()

    def jwk(self) -> dict[str, Any]:
        public = ECAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": public["x"],
            "y": public["y"],
            "kid": self.kid,
            "use": "sig",
            "alg": ALGORITHM,
        }

    def sign(self, claims: dict[str, Any], *, typ: str = TOKEN_TYPE) -> str:
        return jwt.encode(claims, self._key, algorithm=ALGORITHM, headers={"kid": self.kid, "typ": typ})


def server_token_claims(
    *,
    issuer: str,
    account: str,
    client_id: str | None,
    audience: str,
    now: float,
    ttl_s: int = 300,
    device: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """The claims of a per-server token. `device` and `platform` (`did`, `plt`) are given only to a node of the
    Cloud, which needs to know which device is calling; `/v1/server-tokens` leaves them out."""
    issued = int(now)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": account,
        "aud": audience,
        "iat": issued,
        "exp": issued + ttl_s,
        "jti": secrets.token_hex(16),
    }
    if client_id is not None:
        claims["client_id"] = client_id
    if device is not None:
        claims["did"] = device
    if platform is not None:
        claims["plt"] = platform
    return claims
