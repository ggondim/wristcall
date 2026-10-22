"""Cloud mode authentication: the Cloud-signed per-server token (`typ: wc-server+jwt`) is the request principal.

The token names the account (`sub`), the device (`did`), its platform (`plt`) and the token itself (`jti`); all four
are required. Nothing is looked up locally: the Cloud checks the device is still allowed when the node forwards the
token with each request, so the principal is built from the claims alone. The raw token is also kept as the request's
account proof (`current_proof`) for the storage adapter, and is never logged.
"""

import logging

from ..auth import Principal, bearer
from ..oidc import Identity, OidcError, OidcUnavailable, OidcVerifier
from ..storage import Device
from .proof import UserProof, current_proof

log = logging.getLogger("wristcall.cloud_node.auth")

PLATFORMS = ("ios", "ipados", "macos", "watchos", "web")


class CloudAuthenticator:
    def __init__(self, verifier: OidcVerifier) -> None:
        self._verifier = verifier

    async def identify(self, authorization: str | None) -> tuple[Principal, UserProof] | None:
        """The principal and the account proof for this request's token, or None when it is not acceptable.

        Pure: it does not touch `current_proof`. Use `authenticate` to bind the proof to the request.
        """
        token = bearer(authorization)
        if token is None:
            return None
        try:
            identity = await self._verifier.verify(token)
        except OidcError:
            return None
        except OidcUnavailable:
            # Without keys no token can be told from a forgery; the caller answers 401.
            log.warning("cloud keys unavailable; per-server token not checked")
            return None
        return _principal(identity, token)

    async def authenticate(self, authorization: str | None) -> Principal | None:
        # A refusal must never leave an earlier account's proof in this context.
        current_proof.set(None)
        found = await self.identify(authorization)
        if found is None:
            return None
        principal, proof = found
        current_proof.set(proof)
        return principal


def _principal(identity: Identity, token: str) -> tuple[Principal, UserProof] | None:
    did, plt = identity.did, identity.plt
    if did is None or plt is None or identity.jti is None or plt not in PLATFORMS:
        return None
    account = identity.subject
    # Every platform gets a Device so the call path can name who is calling; only the watch is a "device".
    device = Device(id=did, user_id=account, name=plt, created_at=0.0, revoked_at=None)
    principal = Principal(
        user_id=account, kind="device" if plt == "watchos" else "api", device=device, token_id=None, platform=plt
    )
    return principal, UserProof(account=account, device=did, platform=plt, token=token)
