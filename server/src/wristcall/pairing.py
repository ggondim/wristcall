"""Watch ↔ server pairing: 8 digit code bound to a user (flow A) or manual approval (flow B)."""

import asyncio
import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .directory_client import CodeConflict, DirectoryClient, DirectoryError
from .storage import Device, Storage

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
class PendingRequest:
    request_id: str
    device_name: str
    expires_at: float


@dataclass(frozen=True)
class IssuedCode:
    code: PairingCode
    via_directory: bool
    warning: str | None = None


class PairingDenied(Exception):
    pass


class PairingGone(Exception):
    pass


class NotFound(Exception):
    pass


class DeviceLimit(PairingDenied):
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
        storage: Storage,
        approval: Literal["code", "manual"] = "code",
        *,
        now: Callable[[], float] = time.time,
        code_ttl_s: float = 600,
        request_ttl_s: float = 600,
        max_attempts: int = 5,
        max_devices_per_user: int | None = None,
    ) -> None:
        self._st = storage
        self.approval = approval
        self._now = now
        self._code_ttl = code_ttl_s
        self._request_ttl = request_ttl_s
        self._max_attempts = max_attempts
        self._max_devices = max_devices_per_user

    async def _check_device_limit(self, user_id: str) -> None:
        if self._max_devices is not None and await self._st.devices.count(user_id) >= self._max_devices:
            raise DeviceLimit(f"device limit reached ({self._max_devices}); revoke a device first")

    async def create_code(self, user_id: str) -> PairingCode:
        await self._check_device_limit(user_id)
        await self._st.pairing.purge(self._now())
        expires_at = self._now() + self._code_ttl
        for _ in range(10):
            code = f"{secrets.randbelow(10**8):08d}"
            if await self._st.pairing.add_code(code, user_id, expires_at):
                return PairingCode(code=code, expires_at=expires_at)
        raise RuntimeError("could not generate a unique code")

    async def discard_code(self, code: str) -> None:
        await self._st.pairing.discard_code(code)

    async def pair(self, code: str | None, device_name: str) -> Paired | Pending:
        printable = "".join(ch for ch in device_name if ord(ch) >= 0x20 and ord(ch) != 0x7F)
        name = printable.strip()[:64] or "watch"
        now = self._now()
        normalized = normalize_code(code) if code else None
        if normalized is not None:
            user_id = await self._st.pairing.claim_code(normalized, now, self._max_attempts)
            if user_id is not None:
                await self._check_device_limit(user_id)
                return await self._create_device(user_id, name)
        if code:
            await self._st.pairing.count_failed_attempt(now)
        if self.approval == "manual":
            return await self._create_request(name)
        raise PairingDenied("invalid or expired code")

    async def _create_device(self, user_id: str, name: str) -> Paired:
        device_id = secrets.token_hex(4)
        token = secrets.token_urlsafe(32)
        await self._st.devices.create(device_id, user_id, name, hash_secret(token), self._now())
        return Paired(device_id=device_id, token=token)

    async def _create_request(self, name: str) -> Pending:
        now = self._now()
        await self._st.pairing.purge(now)
        taken = await self._st.pairing.pending_ids(now)
        if len(taken) >= MAX_PENDING:
            raise PairingDenied("too many pending requests; try again later")
        request_id = f"{secrets.randbelow(10**4):04d}"
        while request_id in taken:
            request_id = f"{secrets.randbelow(10**4):04d}"
        poll_token = secrets.token_urlsafe(32)
        expires_at = now + self._request_ttl
        await self._st.pairing.add_request(hash_secret(poll_token), request_id, name, now, expires_at)
        return Pending(request_id=request_id, poll_token=poll_token, expires_at=expires_at)

    async def poll(self, poll_token: str) -> Paired | Pending:
        poll_hash = hash_secret(poll_token)
        req = await self._st.pairing.get_request(poll_hash, self._now())
        if req is None:
            raise PairingGone("request does not exist or has expired")
        if req.status == "pending":
            return Pending(request_id=req.request_id, poll_token=poll_token, expires_at=req.expires_at)
        if req.status == "approved" and req.user_id is not None and await self._st.pairing.deliver(poll_hash):
            paired = await self._create_device(req.user_id, req.device_name)
            await self._st.pairing.set_request_device(poll_hash, paired.device_id)
            return paired
        raise PairingGone("request already delivered")

    async def approve(self, request_id: str, user_id: str) -> str:
        now = self._now()
        found = await self._st.pairing.pending_by_id(request_id, now)
        # Exactly one request: with a short_id collision there is no way to know which one the operator saw.
        if len(found) != 1:
            raise NotFound(f"no pending request with id {request_id}")
        await self._check_device_limit(user_id)
        if not await self._st.pairing.approve(found[0].poll_hash, user_id, now, now + self._request_ttl):
            raise NotFound(f"no pending request with id {request_id}")
        return found[0].device_name

    async def authenticate(self, token: str) -> Device | None:
        device = await self._st.devices.by_token(hash_secret(token))
        # A device without an owner (paired by 0.2.0, not adopted yet) cannot call: there is no agent to pick.
        return device if device is not None and device.user_id is not None else None

    async def list_devices(self, user_id: str | None = None) -> list[Device]:
        return await self._st.devices.list(user_id)

    async def list_pending(self) -> list[PendingRequest]:
        return [
            PendingRequest(r.request_id, r.device_name, r.expires_at) for r in await self._st.pairing.list_pending(self._now())
        ]

    async def revoke(self, device_id: str, user_id: str | None = None) -> bool:
        return await self._st.devices.revoke(device_id, self._now(), user_id)


async def issue_code(
    svc: PairingService, user_id: str, public_url: str, directory: DirectoryClient | None
) -> IssuedCode:
    """Creates a code and, with a directory, registers it there (3 tries on conflict).

    Raises DirectoryError if the directory rejects 3 codes in a row; if the directory is unreachable,
    the code still works by typing the URL on the watch (warning set, via_directory False).
    """
    code = await svc.create_code(user_id)
    if directory is None:
        return IssuedCode(code, via_directory=False)
    for attempt in range(3):
        try:
            await asyncio.to_thread(directory.register, public_url, code.code)
            return IssuedCode(code, via_directory=True)
        except CodeConflict:
            await svc.discard_code(code.code)
            if attempt == 2:
                raise DirectoryError("the directory rejected 3 codes in a row; try again") from None
            code = await svc.create_code(user_id)
        except DirectoryError as e:
            return IssuedCode(code, via_directory=False, warning=f"could not register with the directory ({e})")
    raise AssertionError("unreachable")
