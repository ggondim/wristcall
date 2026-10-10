"""Storage interface. Async because the cloud adapter (epic E9) talks to the Cloud API over the network.

Adapters: SQLite (sqlite.py, self-hosted); tests/memory_storage.py proves the interface needs no SQL.
Every method is scoped by the caller; ownership checks (user_id) are passed explicitly so an adapter
never guesses who is asking. Rules that span several records (limits, ordering) are single calls,
so an adapter can make them atomic.

Two groups of methods:
- call path, used on every request (authenticate, agents.get/list, pairing): every adapter implements it;
- operator (marked "Operator"): listing every user or device, adopting orphans, the profile import.
  The SQLite adapter implements it; the Cloud API adapter may raise NotSupported, and a cloud server
  does not run the bootstrap.
"""

from typing import Protocol

from .models import AgentRecord, ApiToken, CallRecord, Device, EntryRecord, PairingRequest, User


class UserStore(Protocol):
    async def create(self, user_id: str, handle: str, display_name: str, now: float) -> User:
        """Raises Conflict if the handle is taken."""
        ...

    async def get(self, user_id: str) -> User | None: ...

    async def by_handle(self, handle: str) -> User | None: ...

    async def list(self) -> list[User]:
        """Operator. Oldest first."""
        ...

    async def update(self, user_id: str, *, handle: str | None = None, display_name: str | None = None) -> User | None:
        """Raises Conflict if the new handle is taken."""
        ...

    async def delete(self, user_id: str) -> bool:
        """Also deletes the user's devices, tokens and agents."""
        ...

    async def link_central(self, user_id: str, subject: str) -> bool:
        """Links the central account `subject` ("<issuer>#<sub>"), replacing the user's previous link.

        False if the user does not exist; raises Conflict if another user has the subject. Idempotent.
        """
        ...

    async def unlink_central(self, user_id: str) -> bool:
        """True if the user had a link."""
        ...

    async def by_central(self, subject: str) -> User | None: ...


class TokenStore(Protocol):
    async def create(self, token_id: str, user_id: str, name: str, token_hash: str, now: float) -> ApiToken: ...

    async def authenticate(self, token_hash: str, now: float) -> ApiToken | None:
        """Active token with this hash. Records last_used_at, at most once a minute (no write per request)."""
        ...

    async def list(self, user_id: str) -> list[ApiToken]:
        """Active tokens, oldest first."""
        ...

    async def revoke(self, token_id: str, now: float) -> bool:
        """Operator scope: any user's token (no user_id yet); a route must check the owner before calling it."""
        ...


class DeviceStore(Protocol):
    async def create(self, device_id: str, user_id: str, name: str, token_hash: str, now: float) -> Device: ...

    async def by_token(self, token_hash: str) -> Device | None:
        """Active device with this token hash."""
        ...

    async def list(self, user_id: str | None = None) -> list[Device]:
        """Active devices of one user, oldest first. user_id None is operator scope (everyone's): routes always pass it."""
        ...

    async def count(self, user_id: str) -> int: ...

    async def revoke(self, device_id: str, now: float, user_id: str | None = None) -> bool:
        """With user_id, only revokes a device of that user. None is operator scope: routes always pass it."""
        ...

    async def adopt_orphans(self, user_id: str) -> int:
        """Operator. Gives devices without an owner (paired by 0.2.0) to this user; returns how many."""
        ...

    async def assign(self, device_id: str, user_id: str) -> bool:
        """Operator. Moves an active device (and its push key) to this user; False if there is no such device."""
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

    async def add_request(
        self, poll_hash: str, request_id: str, device_name: str, now: float, expires_at: float,
        target_user_id: str | None = None,
    ) -> None:
        """target_user_id: the user who must approve it (central account flow); None for the code and manual flows."""
        ...

    async def pending_ids(self, now: float) -> set[str]: ...

    async def get_request(self, poll_hash: str, now: float) -> PairingRequest | None: ...

    async def pending_by_id(self, request_id: str, now: float) -> list[PairingRequest]: ...

    async def approve(self, poll_hash: str, user_id: str, now: float, expires_at: float) -> bool:
        """pending → approved for this user; False if it was no longer pending."""
        ...

    async def deliver(self, poll_hash: str) -> bool:
        """approved → delivered, at most once; False if someone else delivered it."""
        ...

    async def deny(self, poll_hash: str) -> bool:
        """pending → denied; False if it was no longer pending."""
        ...

    async def pending_for(self, target_user_id: str, now: float) -> list[PairingRequest]:
        """Pending, unexpired requests aimed at this user, oldest first."""
        ...

    async def set_request_device(self, poll_hash: str, device_id: str) -> None: ...

    async def list_pending(self, now: float) -> list[PairingRequest]:
        """Operator. Every pending request (manual approval has no target user before approval)."""
        ...


class AgentStore(Protocol):
    async def create(self, record: AgentRecord, max_count: int | None = None) -> AgentRecord:
        """Appends the agent at the end of the user's list (record.position is ignored).

        Raises LimitReached if the user already has max_count agents (checked atomically and first),
        then Conflict on slug.
        """
        ...

    async def get(self, user_id: str, ref: str) -> AgentRecord | None:
        """ref is the agent id or its slug."""
        ...

    async def list(self, user_id: str) -> list[AgentRecord]:
        """By position, then creation."""
        ...

    async def update(self, record: AgentRecord) -> AgentRecord:
        """Replaces every field but id, user_id, created_at and position (position is owned by move).

        Raises Conflict on slug, KeyError if it is gone.
        """
        ...

    async def move(self, user_id: str, agent_id: str, index: int) -> AgentRecord | None:
        """Puts the agent at that index of the user's list (past the end = last) and renumbers 0..n-1, atomically.

        Returns the moved agent, None if it does not exist.
        """
        ...

    async def delete(self, user_id: str, agent_id: str) -> bool: ...

    async def count(self, user_id: str) -> int: ...


class CallStore(Protocol):
    """Calls and their entries (the history). Texts arrive sealed or not and terms arrive computed (history_codec.py):
    the storage keeps what it gets and never sees the key."""

    async def create(self, record: CallRecord) -> CallRecord: ...

    async def get(self, user_id: str, call_id: str) -> CallRecord | None:
        """Only the user's own calls."""
        ...

    async def save(self, record: CallRecord) -> CallRecord:
        """Replaces status, error, attempts, last_http_status, ended_at, finished_at and updated_at. KeyError if gone."""
        ...

    async def interrupt_unfinished(self, now: float) -> int:
        """Operator. At startup: calls left open by a stopped server are closed with error "interrupted"
        (one-way recording or processing → failed; conversation recording → ended). Returns how many."""
        ...

    async def add_entry(self, user_id: str, entry: EntryRecord, terms: list[str]) -> bool:
        """Appends an utterance and indexes its terms. False if the user has no such call (gone meanwhile)."""
        ...

    async def entries(self, user_id: str, call_id: str) -> list[EntryRecord]:
        """By seq; empty if the call is not the user's."""
        ...

    async def entries_by_seal(self, sealed: bool, limit: int) -> list[EntryRecord]:
        """Operator. Entries with text, sealed or not (turning encryption on or off), any order."""
        ...

    async def replace_entry(self, entry: EntryRecord, terms: list[str]) -> bool:
        """Operator. Rewrites the text and sealed flag of the entry (call_id, seq) and its terms. False if gone."""
        ...

    async def list(
        self, user_id: str, *, agent_id: str | None = None, since: float | None = None, until: float | None = None,
        terms: list[list[str]] | None = None, before: tuple[float, str] | None = None, limit: int = 50,
    ) -> list[CallRecord]:
        """The user's calls, newest first (created_at, then id). since <= created_at < until. terms: groups of
        alternatives; a call matches when each group has a term in one of its entries ([] matches nothing).
        before: (created_at, id) of the last call of the previous page; it need not exist any more."""
        ...

    async def delete(self, user_id: str, call_id: str) -> bool: ...

    async def delete_all(self, user_id: str, agent_id: str | None = None) -> int:
        """Every call of the user, or of one of their agents (also a deleted agent's). Returns how many."""
        ...

    async def set_expiry(self, user_id: str, agent_id: str, retention_s: float | None) -> int:
        """expires_at = created_at + retention_s (None: never) on every call of the agent. Returns how many."""
        ...

    async def cap_expiry(self, max_retention_s: float) -> int:
        """Operator. Calls kept longer than created_at + max_retention_s (or forever) get that as expires_at."""
        ...

    async def purge_expired(self, now: float, limit: int = 500) -> int:
        """Operator. Deletes up to `limit` calls whose expires_at has passed, open ones excepted. Returns how many."""
        ...

    async def compact(self) -> None:
        """Operator. Leaves no trace of deleted or replaced text in the storage's files (after encrypt or decrypt)."""
        ...


class PushStore(Protocol):
    """Push relay keys (E6): one per client, a paired device or an API token (a management app)."""

    async def set(
        self, user_id: str, push_key: str, now: float, *, device_id: str | None = None, token_id: str | None = None
    ) -> str | None:
        """Exactly one of device_id, token_id (else ValueError). The client must exist, be active (not revoked) and
        belong to user_id, else KeyError (nothing stored). Replaces the client's key; returns the key it replaced
        (None: it had none, or the same one)."""
        ...

    async def clear(self, *, device_id: str | None = None, token_id: str | None = None) -> str | None:
        """Exactly one of device_id, token_id (else ValueError). Returns the key the client had, now deleted; None
        if it had none."""
        ...

    async def forget(self, push_key: str) -> int:
        """The relay said this key is gone: deletes it wherever it is. Returns how many clients had it."""
        ...

    async def for_device(self, device_id: str) -> str | None:
        """The key of an active (not revoked) device."""
        ...

    async def for_apps(self, user_id: str) -> list[str]:
        """Keys set by the user's active API tokens (management apps), oldest first."""
        ...


class MetaStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...


class Storage(Protocol):
    users: UserStore
    tokens: TokenStore
    devices: DeviceStore
    pairing: PairingStore
    agents: AgentStore
    calls: CallStore
    push: PushStore
    meta: MetaStore

    async def close(self) -> None: ...
