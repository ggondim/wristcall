"""Records exchanged with the storage. Plain data: validation lives in the services above."""

from dataclasses import dataclass
from typing import Any


class StorageError(Exception):
    pass


class Conflict(StorageError):
    """A unique key is already taken (user handle, agent slug per user)."""


class LimitReached(StorageError):
    """The user already has the maximum number of records of that kind."""


class NotSupported(StorageError):
    """An operator-only operation that this adapter does not offer (the Cloud API adapter of epic E9)."""


@dataclass(frozen=True)
class User:
    id: str
    handle: str
    display_name: str
    created_at: float
    central_subject: str | None = None  # "<issuer>#<sub>" of the linked central account


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
    """status: pending → approved → delivered, or pending → denied. target_user_id: set when the request names the
    user who must approve it (central account login); None for the code and manual flows."""

    poll_hash: str
    request_id: str
    device_name: str
    user_id: str | None
    status: str
    expires_at: float
    target_user_id: str | None = None


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


@dataclass(frozen=True)
class CallRecord:
    """One call to a one-shot or monologue agent: its transcript and the delivery to the action URL.

    status: recording (the call is open) → processing (transcribing, delivering) → delivered | failed | empty.
    """

    id: str
    user_id: str
    agent_id: str
    device_id: str | None
    call_type: str
    status: str
    created_at: float
    updated_at: float
    error: str | None = None
    text: str | None = None
    attempts: int = 0
    last_http_status: int | None = None
    ended_at: float | None = None
    finished_at: float | None = None
