"""SQLite adapter of the storage interface (self-hosted). Local and fast: the async methods run inline."""

import json
import sqlite3
from pathlib import Path

from .database import Database, database_path
from .models import AgentRecord, ApiToken, CallRecord, Conflict, Device, EntryRecord, LimitReached, PairingRequest, User

TOKEN_TOUCH_S = 60  # last_used_at precision: one write a minute per token at most


def _unique(e: sqlite3.IntegrityError) -> bool:
    return "UNIQUE" in str(e)


def _user(r: sqlite3.Row) -> User:
    return User(
        id=r["id"], handle=r["handle"], display_name=r["display_name"], created_at=r["created_at"],
        central_subject=r["central_subject"],
    )


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
        user_id=r["user_id"], status=r["status"], expires_at=r["expires_at"], target_user_id=r["target_user_id"],
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

    async def link_central(self, user_id: str, subject: str) -> bool:
        try:
            return self._db.execute("UPDATE users SET central_subject = ? WHERE id = ?", (subject, user_id)) == 1
        except sqlite3.IntegrityError as e:
            if not _unique(e):
                raise
            raise Conflict("central account already linked to another user") from e

    async def unlink_central(self, user_id: str) -> bool:
        return self._db.execute(
            "UPDATE users SET central_subject = NULL WHERE id = ? AND central_subject IS NOT NULL", (user_id,)
        ) == 1

    async def by_central(self, subject: str) -> User | None:
        rows = self._db.query("SELECT * FROM users WHERE central_subject = ?", (subject,))
        return _user(rows[0]) if rows else None


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
        rows = self._db.query("SELECT * FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,))
        if not rows:
            return None
        token = _token(rows[0])
        if token.last_used_at is None or now - token.last_used_at >= TOKEN_TOUCH_S:
            self._db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now, token.id))
            token = ApiToken(**{**token.__dict__, "last_used_at": now})
        return token

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

    async def assign(self, device_id: str, user_id: str) -> bool:
        return self._db.execute(
            "UPDATE devices SET user_id = ? WHERE id = ? AND revoked_at IS NULL", (user_id, device_id)
        ) == 1


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

    async def add_request(
        self, poll_hash: str, request_id: str, device_name: str, now: float, expires_at: float,
        target_user_id: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO pairing_requests (poll_hash, short_id, device_name, created_at, expires_at, status, target_user_id) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (poll_hash, request_id, device_name, now, expires_at, target_user_id),
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

    async def deny(self, poll_hash: str) -> bool:
        return self._db.execute(
            "UPDATE pairing_requests SET status = 'denied' WHERE poll_hash = ? AND status = 'pending'", (poll_hash,)
        ) == 1

    async def pending_for(self, target_user_id: str, now: float) -> list[PairingRequest]:
        rows = self._db.query(
            "SELECT * FROM pairing_requests WHERE target_user_id = ? AND status = 'pending' AND expires_at > ? "
            "ORDER BY created_at",
            (target_user_id, now),
        )
        return [_request(r) for r in rows]

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

    async def create(self, record: AgentRecord, max_count: int | None = None) -> AgentRecord:
        # One statement: the count check and the insert cannot interleave with another writer.
        try:
            rows = self._db.query(
                "INSERT INTO agents (id, user_id, slug, display_name, icon, call_type, position, spec, created_at, updated_at) "
                "SELECT ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM agents WHERE user_id = ?), ?, ?, ? "
                "WHERE ? IS NULL OR (SELECT COUNT(*) FROM agents WHERE user_id = ?) < ? "
                "RETURNING *",
                (
                    record.id, record.user_id, record.slug, record.display_name, record.icon, record.call_type,
                    record.user_id, json.dumps(record.spec), record.created_at, record.updated_at,
                    max_count, record.user_id, max_count,
                ),
            )
        except sqlite3.IntegrityError as e:
            if not _unique(e):
                raise
            raise Conflict(f"agent slug already exists: {record.slug}") from e
        if not rows:
            raise LimitReached(f"agent limit reached ({max_count})")
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
                "UPDATE agents SET slug = ?, display_name = ?, icon = ?, call_type = ?, spec = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ? RETURNING *",
                (
                    record.slug, record.display_name, record.icon, record.call_type, json.dumps(record.spec),
                    record.updated_at, record.id, record.user_id,
                ),
            )
        except sqlite3.IntegrityError as e:
            if not _unique(e):
                raise
            raise Conflict(f"agent slug already exists: {record.slug}") from e
        if not rows:
            raise KeyError(record.id)
        return _agent(rows[0])

    async def move(self, user_id: str, agent_id: str, index: int) -> AgentRecord | None:
        with self._db.transaction() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM agents WHERE user_id = ? ORDER BY position, created_at", (user_id,)
            )]
            if agent_id not in ids:
                return None
            ids.remove(agent_id)
            ids.insert(min(max(index, 0), len(ids)), agent_id)
            for position, each in enumerate(ids):
                conn.execute("UPDATE agents SET position = ? WHERE id = ? AND position != ?", (position, each, position))
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return _agent(row)

    async def delete(self, user_id: str, agent_id: str) -> bool:
        return self._db.execute("DELETE FROM agents WHERE id = ? AND user_id = ?", (agent_id, user_id)) == 1

    async def count(self, user_id: str) -> int:
        return self._db.query("SELECT COUNT(*) FROM agents WHERE user_id = ?", (user_id,))[0][0]


_CALL_FIELDS = ("status", "error", "attempts", "last_http_status", "ended_at", "finished_at", "updated_at")
_CALL_COLUMNS = tuple(CallRecord.__dataclass_fields__)


def _call(r: sqlite3.Row) -> CallRecord:
    return CallRecord(**{k: r[k] for k in _CALL_COLUMNS})


def _entry(r: sqlite3.Row) -> EntryRecord:
    return EntryRecord(
        call_id=r["call_id"], seq=r["seq"], role=r["role"], text=r["text"], sealed=bool(r["sealed"]),
        error=r["error"], at=r["at"],
    )


def fts_query(terms: list[list[str]]) -> str:
    """FTS5 query of term groups: (a OR b) AND (c OR d). Terms are letters and digits only (history_codec.words),
    quoted anyway: nothing the user typed reaches FTS5 syntax."""
    return " AND ".join("(" + " OR ".join(f'"{t}"' for t in group) + ")" for group in terms)


class _Calls:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, record: CallRecord) -> CallRecord:
        self._db.execute(
            f"INSERT INTO calls ({', '.join(_CALL_COLUMNS)}) VALUES ({', '.join('?' for _ in _CALL_COLUMNS)})",
            tuple(getattr(record, n) for n in _CALL_COLUMNS),
        )
        return record

    async def get(self, user_id: str, call_id: str) -> CallRecord | None:
        rows = self._db.query("SELECT * FROM calls WHERE id = ? AND user_id = ?", (call_id, user_id))
        return _call(rows[0]) if rows else None

    async def save(self, record: CallRecord) -> CallRecord:
        rows = self._db.query(
            f"UPDATE calls SET {', '.join(f'{n} = ?' for n in _CALL_FIELDS)} WHERE id = ? RETURNING *",
            (*(getattr(record, n) for n in _CALL_FIELDS), record.id),
        )
        if not rows:
            raise KeyError(record.id)
        return _call(rows[0])

    async def interrupt_unfinished(self, now: float) -> int:
        return self._db.execute(
            "UPDATE calls SET status = CASE WHEN call_type = 'conversation' THEN 'ended' ELSE 'failed' END, "
            "error = 'interrupted', ended_at = COALESCE(ended_at, ?), finished_at = ?, updated_at = ? "
            "WHERE status IN ('recording', 'processing')",
            (now, now, now),
        )

    async def add_entry(self, user_id: str, entry: EntryRecord, terms: list[str]) -> bool:
        with self._db.transaction() as conn:
            row = conn.execute(
                "INSERT INTO call_entries (call_id, seq, role, text, sealed, error, at) "
                "SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM calls WHERE id = ? AND user_id = ?) RETURNING id",
                (entry.call_id, entry.seq, entry.role, entry.text, int(entry.sealed), entry.error, entry.at,
                 entry.call_id, user_id),
            ).fetchone()
            if row is None:
                return False
            if terms:
                conn.execute("INSERT INTO history_fts (rowid, terms) VALUES (?, ?)", (row["id"], " ".join(terms)))
        return True

    async def entries(self, user_id: str, call_id: str) -> list[EntryRecord]:
        rows = self._db.query(
            "SELECT e.* FROM call_entries e JOIN calls c ON c.id = e.call_id WHERE e.call_id = ? AND c.user_id = ? "
            "ORDER BY e.seq",
            (call_id, user_id),
        )
        return [_entry(r) for r in rows]

    async def entries_by_seal(self, sealed: bool, limit: int) -> list[EntryRecord]:
        rows = self._db.query(
            "SELECT * FROM call_entries WHERE text IS NOT NULL AND sealed = ? LIMIT ?", (int(sealed), limit)
        )
        return [_entry(r) for r in rows]

    async def replace_entry(self, entry: EntryRecord, terms: list[str]) -> bool:
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE call_entries SET text = ?, sealed = ? WHERE call_id = ? AND seq = ? RETURNING id",
                (entry.text, int(entry.sealed), entry.call_id, entry.seq),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM history_fts WHERE rowid = ?", (row["id"],))
            if terms:
                conn.execute("INSERT INTO history_fts (rowid, terms) VALUES (?, ?)", (row["id"], " ".join(terms)))
        return True

    async def list(
        self, user_id: str, *, agent_id: str | None = None, since: float | None = None, until: float | None = None,
        terms: list[list[str]] | None = None, before: tuple[float, str] | None = None, limit: int = 50,
    ) -> list[CallRecord]:
        if terms is not None and not terms:
            return []
        where, params = ["c.user_id = ?"], [user_id]
        if agent_id is not None:
            where.append("c.agent_id = ?")
            params.append(agent_id)
        if since is not None:
            where.append("c.created_at >= ?")
            params.append(since)
        if until is not None:
            where.append("c.created_at < ?")
            params.append(until)
        if before is not None:
            where.append("(c.created_at, c.id) < (?, ?)")
            params += list(before)
        for group in terms or []:
            # One condition per word: in a conversation, words may come from different utterances.
            where.append(
                "c.id IN (SELECT e.call_id FROM call_entries e "
                "WHERE e.id IN (SELECT rowid FROM history_fts WHERE history_fts MATCH ?))"
            )
            params.append(fts_query([group]))
        rows = self._db.query(
            f"SELECT c.* FROM calls c WHERE {' AND '.join(where)} ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
            (*params, limit),
        )
        return [_call(r) for r in rows]

    async def delete(self, user_id: str, call_id: str) -> bool:
        return self._db.execute("DELETE FROM calls WHERE id = ? AND user_id = ?", (call_id, user_id)) == 1

    async def delete_all(self, user_id: str, agent_id: str | None = None) -> int:
        if agent_id is None:
            return self._db.execute("DELETE FROM calls WHERE user_id = ?", (user_id,))
        return self._db.execute("DELETE FROM calls WHERE user_id = ? AND agent_id = ?", (user_id, agent_id))

    async def set_expiry(self, user_id: str, agent_id: str, retention_s: float | None) -> int:
        return self._db.execute(
            "UPDATE calls SET expires_at = created_at + ? WHERE user_id = ? AND agent_id = ?",
            (retention_s, user_id, agent_id),
        )

    async def cap_expiry(self, max_retention_s: float) -> int:
        return self._db.execute(
            "UPDATE calls SET expires_at = created_at + ? WHERE expires_at IS NULL OR expires_at > created_at + ?",
            (max_retention_s, max_retention_s),
        )

    async def purge_expired(self, now: float, limit: int = 500) -> int:
        return self._db.execute(
            "DELETE FROM calls WHERE id IN (SELECT id FROM calls WHERE expires_at IS NOT NULL AND expires_at <= ? "
            "AND status NOT IN ('recording', 'processing') LIMIT ?)",
            (now, limit),
        )

    async def compact(self) -> None:
        self._db.compact()



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

    async def delete(self, key: str) -> None:
        self._db.execute("DELETE FROM meta WHERE key = ?", (key,))


class SqliteStorage:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.users = _Users(db)
        self.tokens = _Tokens(db)
        self.devices = _Devices(db)
        self.pairing = _Pairing(db)
        self.agents = _Agents(db)
        self.calls = _Calls(db)
        self.meta = _Meta(db)

    async def close(self) -> None:
        self.db.close()


def open_sqlite_storage(data_dir: Path | str) -> SqliteStorage:
    """The server's database; ':memory:' gives a throwaway one (tests)."""
    path = ":memory:" if str(data_dir) == ":memory:" else database_path(Path(data_dir))
    return SqliteStorage(Database(path))
