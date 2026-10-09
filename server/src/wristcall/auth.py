"""Who is calling: a paired device (calls and lists agents) or a user's API token (manages).

Epic E5 adds the central account's OIDC token as a third kind, resolved here too.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .pairing import hash_secret
from .storage import Device, Storage
from .users import API_TOKEN_PREFIX


@dataclass(frozen=True)
class Principal:
    user_id: str
    kind: Literal["device", "api"]
    device: Device | None = None
    token_id: str | None = None


def bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


class Authenticator:
    def __init__(self, storage: Storage, *, now: Callable[[], float] = time.time) -> None:
        self._st = storage
        self._now = now

    async def authenticate(self, authorization: str | None) -> Principal | None:
        token = bearer(authorization)
        if token is None:
            return None
        if token.startswith(API_TOKEN_PREFIX):
            record = await self._st.tokens.authenticate(hash_secret(token), self._now())
            return Principal(user_id=record.user_id, kind="api", token_id=record.id) if record else None
        device = await self._st.devices.by_token(hash_secret(token))
        # A device without an owner (paired by 0.2.0, not adopted yet) cannot call: there is no agent to pick.
        if device is None or device.user_id is None:
            return None
        return Principal(user_id=device.user_id, kind="device", device=device)
