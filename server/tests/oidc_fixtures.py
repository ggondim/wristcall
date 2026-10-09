"""Test issuer: RSA keys, a mocked discovery document and JWKS, and signed tokens."""

import time
from typing import Any

import httpx
import jwt
import respx
from cryptography.hazmat.primitives.asymmetric import ec, rsa

ISSUER = "https://issuer.test"
JWKS_URI = f"{ISSUER}/oauth/v2/keys"
AUDIENCE = "client-watch"


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(private, kid: str, alg: str = "RS256") -> dict[str, Any]:
    if alg.startswith("ES"):
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(private.public_key(), as_dict=True)
    else:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update(kid=kid, alg=alg, use="sig")
    return jwk


def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


class FakeIssuer:
    """Holds the signing keys and serves them through respx routes."""

    def __init__(self, router: respx.Router, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.keys: dict[str, tuple[Any, str]] = {"k1": (rsa_key(), "RS256")}
        self.discovery = router.get(f"{issuer}/.well-known/openid-configuration").mock(
            side_effect=lambda request: httpx.Response(200, json={"issuer": issuer, "jwks_uri": f"{issuer}/oauth/v2/keys"})
        )
        self.jwks = router.get(f"{issuer}/oauth/v2/keys").mock(
            side_effect=lambda request: httpx.Response(
                200, json={"keys": [public_jwk(k, kid, alg) for kid, (k, alg) in self.keys.items()]}
            )
        )

    def token(self, kid: str = "k1", *, alg: str | None = None, headers: dict | None = None, **overrides: Any) -> str:
        now = time.time()
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": "central-user-1",
            "aud": [AUDIENCE],
            "iat": now,
            "exp": now + 600,
            "client_id": AUDIENCE,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        key, key_alg = self.keys[kid]
        return jwt.encode(claims, key, algorithm=alg or key_alg, headers={"kid": kid, **(headers or {})})
