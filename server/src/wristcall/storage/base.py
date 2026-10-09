"""Storage interface. Async because the cloud adapter (epic E9) talks to the Cloud API over the network.

Adapters: SQLite (sqlite.py, self-hosted). Every method is scoped by the caller; ownership checks
(user_id) are passed explicitly so an adapter never guesses who is asking.
"""

from typing import Protocol

from .models import AgentRecord, ApiToken, Device, PairingRequest, User


class UserStore(Protocol):
    async def create(self, user_id: str, handle: str, display_name: str, now: float) -> User:
        """Raises Conflict if the handle is taken."""
        ...

    async def get(self, user_id: str) -> User | None: ...

    async def by_handle(self, handle: str) -> User | None: ...

    async def list(self) -> list[User]:
        """Oldest first."""
        ...

    async def update(self, user_id: str, *, handle: str | None = None, display_name: str | None = None) -> User | None:
        """Raises Conflict if the new handle is taken."""
        ...

    async def delete(self, user_id: str) -> bool:
        """Also deletes the user's devices, tokens and agents."""
        ...


class TokenStore(Protocol):
    async def create(self, token_id: str, user_id: str, name: str, token_hash: str, now: float) -> ApiToken: ...

    async def authenticate(self, token_hash: str, now: float) -> ApiToken | None:
        """Active token with this hash; records last_used_at."""
        ...

    async def list(self, user_id: str) -> list[ApiToken]:
        """Active tokens, oldest first."""
        ...

    async def revoke(self, token_id: str, now: float) -> bool: ...


class DeviceStore(Protocol):
    async def create(self, device_id: str, user_id: str, name: str, token_hash: str, now: float) -> Device: ...

    async def by_token(self, token_hash: str) -> Device | None:
        """Active device with this token hash."""
        ...

    async def list(self, user_id: str | None = None) -> list[Device]:
        """Active devices (of one user, or all), oldest first."""
        ...

    async def count(self, user_id: str) -> int: ...

    async def revoke(self, device_id: str, now: float, user_id: str | None = None) -> bool:
        """With user_id, only revokes a device of that user."""
        ...

    async def adopt_orphans(self, user_id: str) -> int:
        """Gives devices without an owner (paired by 0.2.0) to this user; returns how many."""
        ...


class PairingStore(Protocol):
    async def purge(self, now: float) -> None:
        """Deletes expired codes and requests."""
        ...

    async def add_code(self, code: str, user_id: str, expires_at: float) -> bool:
        """False if the code already exists."""
        ...

    async def discard_code(self, code: str) -> None: ...

    async def claim_code(self, code: str, now: float, max_attempts: int) -> str | None:
        """Marks a valid code as used and returns its owner; None if invalid, used, expired or blocked."""
        ...

    async def count_failed_attempt(self, now: float) -> None:
        """A wrong code was tried: every active code gets one attempt closer to being blocked."""
        ...

    async def add_request(self, poll_hash: str, request_id: str, device_name: str, now: float, expires_at: float) -> None: ...

    async def pending_ids(self, now: float) -> set[str]: ...

    async def get_request(self, poll_hash: str, now: float) -> PairingRequest | None: ...

    async def pending_by_id(self, request_id: str, now: float) -> list[PairingRequest]: ...

    async def approve(self, poll_hash: str, user_id: str, now: float, expires_at: float) -> bool:
        """pending → approved for this user; False if it was no longer pending."""
        ...

    async def deliver(self, poll_hash: str) -> bool:
        """approved → delivered, at most once; False if someone else delivered it."""
        ...

    async def set_request_device(self, poll_hash: str, device_id: str) -> None: ...

    async def list_pending(self, now: float) -> list[PairingRequest]: ...


class AgentStore(Protocol):
    async def create(self, record: AgentRecord) -> AgentRecord:
        """Appends the agent at the end of the user's list (record.position is ignored). Raises Conflict on slug."""
        ...

    async def get(self, user_id: str, ref: str) -> AgentRecord | None:
        """ref is the agent id or its slug."""
        ...

    async def list(self, user_id: str) -> list[AgentRecord]:
        """By position, then creation."""
        ...

    async def update(self, record: AgentRecord) -> AgentRecord:
        """Replaces every field but id, user_id and created_at. Raises Conflict on slug, KeyError if it is gone."""
        ...

    async def delete(self, user_id: str, agent_id: str) -> bool: ...

    async def count(self, user_id: str) -> int: ...


class MetaStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...


class Storage(Protocol):
    users: UserStore
    tokens: TokenStore
    devices: DeviceStore
    pairing: PairingStore
    agents: AgentStore
    meta: MetaStore

    async def close(self) -> None: ...
