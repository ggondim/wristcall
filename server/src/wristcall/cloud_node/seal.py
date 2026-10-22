"""Sealing of the user's agent endpoints in cloud mode (plan O3c, decision D5).

An endpoint the user wrote (a dict with `type`: base_url, api_key, headers...) is secret. The Cloud keeps the agent
`spec` in Mongo, so each such stage is stored as `{"sealed": "v1.<b64url(nonce12 + ciphertext)>"}`: AES-256-GCM
under a key derived from `seal_key`, bound to the account, the agent and the stage. The Cloud never holds the key.

What the operator offers stays open: `{"provider": name}` and the rest of the spec. Reading is strict: a stage that
comes back from the Cloud is a sealed envelope, exactly `{"provider": name}`, or null. A clear-text endpoint is
refused, so whoever can write to Mongo cannot point the audio anywhere else.
"""

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

STAGES = ("stt", "action", "tts")
KEY_BYTES = 32
NONCE_BYTES = 12
PREFIX = "v1."
_INFO = b"wristcall agent seal v1"


class SealError(ValueError):
    """A sealed stage cannot be opened (another account, agent, stage or key; damaged), or a stage is not allowed."""


def seal_aad(account: str, agent_id: str, stage: str) -> str:
    """What a sealed endpoint is bound to. Account ids never hold `|`, which keeps the fields apart."""
    if "|" in account:
        raise SealError("account id must not contain '|'")
    return f"{account}|{agent_id}|{stage}"


def _is_provider_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"provider"} and isinstance(value["provider"], str)


class SealCodec:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("seal key must be 32 bytes")
        self._aead = AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(key))

    def seal_spec(self, spec: dict[str, Any], account: str, agent_id: str) -> dict[str, Any]:
        """A copy of the spec with each user endpoint sealed. Raises SealError for a stage that is neither an
        endpoint (has `type`), nor `{"provider": ...}`, nor null, and for an account id holding `|`."""
        sealed = dict(spec)
        for stage in STAGES:
            value = spec.get(stage)
            if value is None or _is_provider_ref(value):
                continue
            if not isinstance(value, dict) or "type" not in value:
                raise SealError(f"{stage}: not an endpoint")
            aad = seal_aad(account, agent_id, stage).encode()
            plain = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
            nonce = os.urandom(NONCE_BYTES)
            token = base64.urlsafe_b64encode(nonce + self._aead.encrypt(nonce, plain, aad)).decode().rstrip("=")
            sealed[stage] = {"sealed": PREFIX + token}
        return sealed

    def open_spec(self, spec: dict[str, Any], account: str, agent_id: str) -> dict[str, Any]:
        """A copy of the spec with each sealed stage opened. Raises SealError for anything else but an open
        `{"provider": ...}` or null (a clear-text endpoint included)."""
        if not isinstance(spec, dict):
            raise SealError("spec is not an object")
        opened = dict(spec)
        for stage in STAGES:
            value = spec.get(stage)
            if value is None or _is_provider_ref(value):
                continue
            if isinstance(value, dict) and set(value) == {"sealed"}:
                opened[stage] = self._open(value["sealed"], account, agent_id, stage)
            else:
                raise SealError(f"{stage}: not sealed")
        return opened

    def _open(self, token: Any, account: str, agent_id: str, stage: str) -> dict[str, Any]:
        if not isinstance(token, str) or not token.startswith(PREFIX):
            raise SealError(f"{stage}: unknown seal format")
        body = token[len(PREFIX):]
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            if len(raw) <= NONCE_BYTES:
                raise ValueError
            plain = self._aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], seal_aad(account, agent_id, stage).encode())
            endpoint = json.loads(plain)
        except (InvalidTag, ValueError, binascii.Error):
            raise SealError(f"{stage}: cannot be opened (another account, agent or key; or damaged)") from None
        if not isinstance(endpoint, dict) or "type" not in endpoint:
            raise SealError(f"{stage}: not an endpoint")
        return endpoint
