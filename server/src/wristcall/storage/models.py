"""Records exchanged with the storage. Plain data: validation lives in the services above."""

from dataclasses import dataclass
from typing import Any


class StorageError(Exception):
    pass


class Conflict(StorageError):
    """A unique key is already taken (user handle, agent slug per user)."""


@dataclass(frozen=True)
class User:
    id: str
    handle: str
    display_name: str
    created_at: float


@dataclass(frozen=True)
class ApiToken:
    id: str
    user_id: str
    name: str
    created_at: float
    last_used_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class Device:
    id: str
    user_id: str | None
    name: str
    created_at: float
    revoked_at: float | None


@dataclass(frozen=True)
class PairingRequest:
    poll_hash: str
    request_id: str
    device_name: str
    user_id: str | None
    status: str
    expires_at: float


@dataclass(frozen=True)
class AgentRecord:
    id: str
    user_id: str
    slug: str
    display_name: str
    icon: str
    call_type: str
    position: int
    spec: dict[str, Any]
    created_at: float
    updated_at: float
