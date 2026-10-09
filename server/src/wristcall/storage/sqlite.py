"""SQLite adapter of the storage interface (self-hosted). Local and fast: the async methods run inline."""

import json
import sqlite3
from pathlib import Path

from .database import Database, database_path
from .models import AgentRecord, ApiToken, Conflict, Device, PairingRequest, User


def _unique(e: sqlite3.IntegrityError) -> bool:
    return "UNIQUE" in str(e)


def _user(r: sqlite3.Row) -> User:
    return User(id=r["id"], handle=r["handle"], display_name=r["display_name"], created_at=r["created_at"])


def _token(r: sqlite3.Row) -> ApiToken:
    return ApiToken(
        id=r["id"], user_id=r["user_id"], name=r["name"], created_at=r["created_at"],
        last_used_at=r["last_used_at"], revoked_at=r["revoked_at"],
    )


def _device(r: sqlite3.Row) -> Device:
    return Device(id=r["id"], user_id=r["user_id"], name=r["name"], created_at=r["created_at"], revoked_at=r["revoked_at"])


def _request(r: sqlite3.Row) -> PairingRequest:
    return PairingRequest(
        poll_hash=r["poll_hash"], request_id=r["short_id"], device_name=r["device_name"],
        user_id=r["user_id"], status=r["status"], expires_at=r["expires_at"],
    )


def _agent(r: sqlite3.Row) -> AgentRecord:
    return AgentRecord(
        id=r["id"], user_id=r["user_id"], slug=r["slug"], display_name=r["display_name"], icon=r["icon"],
        call_type=r["call_type"], position=r["position"], spec=json.loads(r["spec"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


class _Users:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, user_id: str, handle: str, display_name: str, now: float) -> User:
        try:
            self._db.execute(
                "INSERT INTO users (id, handle, display_name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, handle, display_name, now),
            )
        except sqlite3.IntegrityError as e:
            if not _unique(e):
                raise
            raise Conflict(f"user handle already exists: {handle}") from e
        return User(id=user_id, handle=handle, display_name=display_name, created_at=now)

    async def get(self, user_id: str) -> User | None:
        rows = self._db.query("SELECT * FROM users WHERE id = ?", (user_id,))
        return _user(rows[0]) if rows else None

    async def by_handle(self, handle: str) -> User | None:
        rows = self._db.query("SELECT * FROM users WHERE handle = ?", (handle,))
        return _user(rows[0]) if rows else None

    async def list(self) -> list[User]:
        return [_user(r) for r in self._db.query("SELECT * FROM users ORDER BY created_at, handle")]

    async def update(self, user_id: str, *, handle: str | None = None, display_name: str | None = None) -> User | None:
        try:
            self._db.execute(
                "UPDATE users SET handle = COALESCE(?, handle), display_name = COALESCE(?, display_name) WHERE id = ?",
                (handle, display_name, user_id),
            )
        except sqlite3.IntegrityError as e:
            if not _unique(e):
                raise
            raise Conflict(f"user handle already exists: {handle}") from e
        return await self.get(user_id)

    async def delete(self, user_id: str) -> bool:
        return self._db.execute("DELETE FROM users WHERE id = ?", (user_id,)) == 1


class _Tokens:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, token_id: str, user_id: str, name: str, token_hash: str, now: float) -> ApiToken:
        self._db.execute(
            "INSERT INTO api_tokens (id, user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (token_id, user_id, name, token_hash, now),
        )
        return ApiToken(id=token_id, user_id=user_id, name=name, created_at=now, last_used_at=None, revoked_at=None)

    async def authenticate(self, token_hash: str, now: float) -> ApiToken | None:
        rows = self._db.query(
            "UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ? AND revoked_at IS NULL RETURNING *",
            (now, token_hash),
        )
        return _token(rows[0]) if rows else None

    async def list(self, user_id: str) -> list[ApiToken]:
        rows = self._db.query(
            "SELECT * FROM api_tokens WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at", (user_id,)
        )
        return [_token(r) for r in rows]

    async def revoke(self, token_id: str, now: float) -> bool:
        return self._db.execute(
            "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, token_id)
        ) == 1


class _Devices:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, device_id: str, user_id: str, name: str, token_hash: str, now: float) -> Device:
        self._db.execute(
            "INSERT INTO devices (id, user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (device_id, user_id, name, token_hash, now),
        )
        return Device(id=device_id, user_id=user_id, name=name, created_at=now, revoked_at=None)

    async def by_token(self, token_hash: str) -> Device | None:
        rows = self._db.query("SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,))
        return _device(rows[0]) if rows else None

    async def list(self, user_id: str | None = None) -> list[Device]:
        if user_id is None:
            rows = self._db.query("SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY created_at")
        else:
            rows = self._db.query(
                "SELECT * FROM devices WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at", (user_id,)
            )
        return [_device(r) for r in rows]

    async def count(self, user_id: str) -> int:
        return self._db.query("SELECT COUNT(*) FROM devices WHERE user_id = ? AND revoked_at IS NULL", (user_id,))[0][0]

    async def revoke(self, device_id: str, now: float, user_id: str | None = None) -> bool:
        if user_id is None:
            sql, params = "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, device_id)
        else:
            sql = "UPDATE devices SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL"
            params = (now, device_id, user_id)
        return self._db.execute(sql, params) == 1

    async def adopt_orphans(self, user_id: str) -> int:
        return self._db.execute("UPDATE devices SET user_id = ? WHERE user_id IS NULL", (user_id,))


class _Pairing:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def purge(self, now: float) -> None:
        self._db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
        self._db.execute("DELETE FROM pairing_requests WHERE expires_at < ?", (now,))

    async def add_code(self, code: str, user_id: str, expires_at: float) -> bool:
        try:
            self._db.execute(
                "INSERT INTO pairing_codes (code, user_id, expires_at) VALUES (?, ?, ?)", (code, user_id, expires_at)
            )
        except sqlite3.IntegrityError:
            return False
        return True

    async def discard_code(self, code: str) -> None:
        self._db.execute("DELETE FROM pairing_codes WHERE code = ?", (code,))

    async def claim_code(self, code: str, now: float, max_attempts: int) -> str | None:
        rows = self._db.query(
            "UPDATE pairing_codes SET used_at = ? "
            "WHERE code = ? AND used_at IS NULL AND expires_at > ? AND attempts < ? AND user_id IS NOT NULL "
            "RETURNING user_id",
            (now, code, now, max_attempts),
        )
        return rows[0]["user_id"] if rows else None

    async def count_failed_attempt(self, now: float) -> None:
        self._db.execute("UPDATE pairing_codes SET attempts = attempts + 1 WHERE used_at IS NULL AND expires_at > ?", (now,))

    async def add_request(self, poll_hash: str, request_id: str, device_name: str, now: float, expires_at: float) -> None:
        self._db.execute(
            "INSERT INTO pairing_requests (poll_hash, short_id, device_name, created_at, expires_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (poll_hash, request_id, device_name, now, expires_at),
        )

    async def pending_ids(self, now: float) -> set[str]:
        rows = self._db.query("SELECT short_id FROM pairing_requests WHERE status = 'pending' AND expires_at > ?", (now,))
        return {r["short_id"] for r in rows}

    async def get_request(self, poll_hash: str, now: float) -> PairingRequest | None:
        rows = self._db.query("SELECT * FROM pairing_requests WHERE poll_hash = ? AND expires_at > ?", (poll_hash, now))
        return _request(rows[0]) if rows else None

    async def pending_by_id(self, request_id: str, now: float) -> list[PairingRequest]:
        rows = self._db.query(
            "SELECT * FROM pairing_requests WHERE short_id = ? AND status = 'pending' AND expires_at > ?",
            (request_id, now),
        )
        return [_request(r) for r in rows]

    async def approve(self, poll_hash: str, user_id: str, now: float, expires_at: float) -> bool:
        return self._db.execute(
            "UPDATE pairing_requests SET status = 'approved', user_id = ?, expires_at = ? "
            "WHERE poll_hash = ? AND status = 'pending' AND expires_at > ?",
            (user_id, expires_at, poll_hash, now),
        ) == 1

    async def deliver(self, poll_hash: str) -> bool:
        return self._db.execute(
            "UPDATE pairing_requests SET status = 'delivered' WHERE poll_hash = ? AND status = 'approved'", (poll_hash,)
        ) == 1

    async def set_request_device(self, poll_hash: str, device_id: str) -> None:
        self._db.execute("UPDATE pairing_requests SET device_id = ? WHERE poll_hash = ?", (device_id, poll_hash))

    async def list_pending(self, now: float) -> list[PairingRequest]:
        rows = self._db.query(
            "SELECT * FROM pairing_requests WHERE status = 'pending' AND expires_at > ? ORDER BY created_at", (now,)
        )
        return [_request(r) for r in rows]


class _Agents:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, record: AgentRecord) -> AgentRecord:
        try:
            rows = self._db.query(
                "INSERT INTO agents (id, user_id, slug, display_name, icon, call_type, position, spec, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM agents WHERE user_id = ?), ?, ?, ?) "
                "RETURNING *",
                (
                    record.id, record.user_id, record.slug, record.display_name, record.icon, record.call_type,
                    record.user_id, json.dumps(record.spec), record.created_at, record.updated_at,
                ),
            )
        except sqlite3.IntegrityError as e:
            if not _unique(e):
                raise
            raise Conflict(f"agent slug already exists: {record.slug}") from e
        return _agent(rows[0])

    async def get(self, user_id: str, ref: str) -> AgentRecord | None:
        rows = self._db.query("SELECT * FROM agents WHERE user_id = ? AND (id = ? OR slug = ?)", (user_id, ref, ref))
        return _agent(rows[0]) if rows else None

    async def list(self, user_id: str) -> list[AgentRecord]:
        rows = self._db.query("SELECT * FROM agents WHERE user_id = ? ORDER BY position, created_at", (user_id,))
        return [_agent(r) for r in rows]

    async def update(self, record: AgentRecord) -> AgentRecord:
        try:
            rows = self._db.query(
                "UPDATE agents SET slug = ?, display_name = ?, icon = ?, call_type = ?, position = ?, spec = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ? RETURNING *",
                (
                    record.slug, record.display_name, record.icon, record.call_type, record.position,
                    json.dumps(record.spec), record.updated_at, record.id, record.user_id,
                ),
            )
        except sqlite3.IntegrityError as e:
            if not _unique(e):
                raise
            raise Conflict(f"agent slug already exists: {record.slug}") from e
        if not rows:
            raise KeyError(record.id)
        return _agent(rows[0])

    async def delete(self, user_id: str, agent_id: str) -> bool:
        return self._db.execute("DELETE FROM agents WHERE id = ? AND user_id = ?", (agent_id, user_id)) == 1

    async def count(self, user_id: str) -> int:
        return self._db.query("SELECT COUNT(*) FROM agents WHERE user_id = ?", (user_id,))[0][0]


class _Meta:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> str | None:
        rows = self._db.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    async def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


class SqliteStorage:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.users = _Users(db)
        self.tokens = _Tokens(db)
        self.devices = _Devices(db)
        self.pairing = _Pairing(db)
        self.agents = _Agents(db)
        self.meta = _Meta(db)

    async def close(self) -> None:
        self.db.close()


def open_sqlite_storage(data_dir: Path | str) -> SqliteStorage:
    """The server's database; ':memory:' gives a throwaway one (tests)."""
    path = ":memory:" if str(data_dir) == ":memory:" else database_path(Path(data_dir))
    return SqliteStorage(Database(path))
