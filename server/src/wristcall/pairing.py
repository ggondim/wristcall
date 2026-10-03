"""Watch ↔ server pairing: 8 digit code (flow A) or manual approval (flow B)."""

import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .store import Database

MAX_PENDING = 20


@dataclass(frozen=True)
class PairingCode:
    code: str
    expires_at: float


@dataclass(frozen=True)
class Paired:
    device_id: str
    token: str


@dataclass(frozen=True)
class Pending:
    request_id: str
    poll_token: str
    expires_at: float


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created_at: float
    revoked_at: float | None


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    device_name: str
    expires_at: float


class PairingDenied(Exception):
    pass


class PairingGone(Exception):
    pass


class NotFound(Exception):
    pass


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def format_code(code: str) -> str:
    return f"{code[:4]} {code[4:]}"


def normalize_code(raw: str) -> str | None:
    cleaned = re.sub(r"[\s-]", "", raw)
    return cleaned if re.fullmatch(r"[0-9]{8}", cleaned) else None


class PairingService:
    def __init__(
        self,
        db: Database,
        approval: Literal["code", "manual"] = "code",
        *,
        now: Callable[[], float] = time.time,
        code_ttl_s: float = 600,
        request_ttl_s: float = 600,
        max_attempts: int = 5,
    ) -> None:
        self._db = db
        self.approval = approval
        self._now = now
        self._code_ttl = code_ttl_s
        self._request_ttl = request_ttl_s
        self._max_attempts = max_attempts

    def _purge(self) -> None:
        now = self._now()
        self._db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
        self._db.execute("DELETE FROM pairing_requests WHERE expires_at < ?", (now,))

    def create_code(self) -> PairingCode:
        self._purge()
        expires_at = self._now() + self._code_ttl
        for _ in range(10):
            code = f"{secrets.randbelow(10**8):08d}"
            if self._db.query("SELECT 1 FROM pairing_codes WHERE code = ?", (code,)):
                continue
            self._db.execute("INSERT INTO pairing_codes (code, expires_at) VALUES (?, ?)", (code, expires_at))
            return PairingCode(code=code, expires_at=expires_at)
        raise RuntimeError("could not generate a unique code")

    def discard_code(self, code: str) -> None:
        self._db.execute("DELETE FROM pairing_codes WHERE code = ?", (code,))

    def pair(self, code: str | None, device_name: str) -> Paired | Pending:
        printable = "".join(ch for ch in device_name if ord(ch) >= 0x20 and ord(ch) != 0x7F)
        name = printable.strip()[:64] or "watch"
        now = self._now()
        normalized = normalize_code(code) if code else None
        if normalized is not None:
            used = self._db.execute(
                "UPDATE pairing_codes SET used_at = ? "
                "WHERE code = ? AND used_at IS NULL AND expires_at > ? AND attempts < ?",
                (now, normalized, now, self._max_attempts),
            )
            if used == 1:
                return self._create_device(name)
        if code:
            self._db.execute(
                "UPDATE pairing_codes SET attempts = attempts + 1 WHERE used_at IS NULL AND expires_at > ?",
                (now,),
            )
        if self.approval == "manual":
            return self._create_request(name)
        raise PairingDenied("invalid or expired code")

    def _create_device(self, name: str) -> Paired:
        device_id = secrets.token_hex(4)
        token = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO devices (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (device_id, name, hash_secret(token), self._now()),
        )
        return Paired(device_id=device_id, token=token)

    def _create_request(self, name: str) -> Pending:
        self._purge()
        now = self._now()
        rows = self._db.query("SELECT short_id FROM pairing_requests WHERE status = 'pending' AND expires_at > ?", (now,))
        taken = {r["short_id"] for r in rows}
        if len(taken) >= MAX_PENDING:
            raise PairingDenied("too many pending requests; try again later")
        request_id = f"{secrets.randbelow(10**4):04d}"
        while request_id in taken:
            request_id = f"{secrets.randbelow(10**4):04d}"
        poll_token = secrets.token_urlsafe(32)
        expires_at = now + self._request_ttl
        self._db.execute(
            "INSERT INTO pairing_requests (poll_hash, short_id, device_name, created_at, expires_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (hash_secret(poll_token), request_id, name, now, expires_at),
        )
        return Pending(request_id=request_id, poll_token=poll_token, expires_at=expires_at)

    def poll(self, poll_token: str) -> Paired | Pending:
        now = self._now()
        poll_hash = hash_secret(poll_token)
        rows = self._db.query("SELECT * FROM pairing_requests WHERE poll_hash = ? AND expires_at > ?", (poll_hash, now))
        if not rows:
            raise PairingGone("request does not exist or has expired")
        row = rows[0]
        if row["status"] == "pending":
            return Pending(request_id=row["short_id"], poll_token=poll_token, expires_at=row["expires_at"])
        if row["status"] == "approved":
            claimed = self._db.execute(
                "UPDATE pairing_requests SET status = 'delivered' WHERE poll_hash = ? AND status = 'approved'",
                (poll_hash,),
            )
            if claimed == 1:
                paired = self._create_device(row["device_name"])
                self._db.execute("UPDATE pairing_requests SET device_id = ? WHERE poll_hash = ?", (paired.device_id, poll_hash))
                return paired
        raise PairingGone("request already delivered")

    def approve(self, request_id: str) -> str:
        now = self._now()
        rows = self._db.query(
            "SELECT poll_hash, device_name FROM pairing_requests WHERE short_id = ? AND status = 'pending' AND expires_at > ?",
            (request_id, now),
        )
        # Exactly one request: with a short_id collision there is no way to know which one the operator saw.
        if len(rows) != 1:
            raise NotFound(f"no pending request with id {request_id}")
        approved = self._db.execute(
            "UPDATE pairing_requests SET status = 'approved', expires_at = ? "
            "WHERE poll_hash = ? AND short_id = ? AND status = 'pending' AND expires_at > ?",
            (now + self._request_ttl, rows[0]["poll_hash"], request_id, now),
        )
        if approved != 1:
            raise NotFound(f"no pending request with id {request_id}")
        return rows[0]["device_name"]

    def authenticate(self, token: str) -> Device | None:
        rows = self._db.query("SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", (hash_secret(token),))
        return self._device(rows[0]) if rows else None

    def list_devices(self) -> list[Device]:
        rows = self._db.query("SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY created_at")
        return [self._device(r) for r in rows]

    def list_pending(self) -> list[PendingRequest]:
        rows = self._db.query(
            "SELECT short_id, device_name, expires_at FROM pairing_requests "
            "WHERE status = 'pending' AND expires_at > ? ORDER BY created_at",
            (self._now(),),
        )
        return [PendingRequest(r["short_id"], r["device_name"], r["expires_at"]) for r in rows]

    def revoke(self, device_id: str) -> bool:
        return self._db.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (self._now(), device_id)
        ) == 1

    @staticmethod
    def _device(row) -> Device:
        return Device(id=row["id"], name=row["name"], created_at=row["created_at"], revoked_at=row["revoked_at"])
