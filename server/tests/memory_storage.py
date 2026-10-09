"""Storage made of dicts: runs the contract tests (test_storage.py) next to the SQLite adapter.

It proves the interface in wristcall/storage/base.py needs no SQL, which the Cloud API adapter of
epic E9 relies on. Not shipped: it lives in tests/.
"""

from dataclasses import replace

from wristcall.storage import AgentRecord, ApiToken, CallRecord, Conflict, Device, LimitReached, PairingRequest, User

TOKEN_TOUCH_S = 60


class _Users:
    def __init__(self, root: "MemoryStorage") -> None:
        self._root = root
        self.rows: dict[str, User] = {}

    async def create(self, user_id, handle, display_name, now):
        if any(u.handle == handle for u in self.rows.values()):
            raise Conflict(f"user handle already exists: {handle}")
        self.rows[user_id] = User(user_id, handle, display_name, now)
        return self.rows[user_id]

    async def get(self, user_id):
        return self.rows.get(user_id)

    async def by_handle(self, handle):
        return next((u for u in self.rows.values() if u.handle == handle), None)

    async def list(self):
        return sorted(self.rows.values(), key=lambda u: (u.created_at, u.handle))

    async def update(self, user_id, *, handle=None, display_name=None):
        user = self.rows.get(user_id)
        if user is None:
            return None
        if handle is not None and any(u.handle == handle and u.id != user_id for u in self.rows.values()):
            raise Conflict(f"user handle already exists: {handle}")
        self.rows[user_id] = replace(user, handle=handle or user.handle, display_name=display_name or user.display_name)
        return self.rows[user_id]

    async def delete(self, user_id):
        if self.rows.pop(user_id, None) is None:
            return False
        self._root.cascade(user_id)
        return True


class _Tokens:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[ApiToken, str]] = {}

    async def create(self, token_id, user_id, name, token_hash, now):
        self.rows[token_id] = (ApiToken(token_id, user_id, name, now, None, None), token_hash)
        return self.rows[token_id][0]

    async def authenticate(self, token_hash, now):
        for token, h in self.rows.values():
            if h == token_hash and token.revoked_at is None:
                if token.last_used_at is None or now - token.last_used_at >= TOKEN_TOUCH_S:
                    token = replace(token, last_used_at=now)
                    self.rows[token.id] = (token, h)
                return token
        return None

    async def list(self, user_id):
        return sorted((t for t, _ in self.rows.values() if t.user_id == user_id and t.revoked_at is None), key=lambda t: t.created_at)

    async def revoke(self, token_id, now):
        entry = self.rows.get(token_id)
        if entry is None or entry[0].revoked_at is not None:
            return False
        self.rows[token_id] = (replace(entry[0], revoked_at=now), entry[1])
        return True


class _Devices:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[Device, str]] = {}

    def add_orphan(self, device_id, name, token_hash, now):
        self.rows[device_id] = (Device(device_id, None, name, now, None), token_hash)

    async def create(self, device_id, user_id, name, token_hash, now):
        self.rows[device_id] = (Device(device_id, user_id, name, now, None), token_hash)
        return self.rows[device_id][0]

    async def by_token(self, token_hash):
        return next((d for d, h in self.rows.values() if h == token_hash and d.revoked_at is None), None)

    async def list(self, user_id=None):
        found = [d for d, _ in self.rows.values() if d.revoked_at is None and (user_id is None or d.user_id == user_id)]
        return sorted(found, key=lambda d: d.created_at)

    async def count(self, user_id):
        return len(await self.list(user_id))

    async def revoke(self, device_id, now, user_id=None):
        entry = self.rows.get(device_id)
        if entry is None or entry[0].revoked_at is not None or (user_id is not None and entry[0].user_id != user_id):
            return False
        self.rows[device_id] = (replace(entry[0], revoked_at=now), entry[1])
        return True

    async def adopt_orphans(self, user_id):
        orphans = [k for k, (d, _) in self.rows.items() if d.user_id is None]
        for k in orphans:
            d, h = self.rows[k]
            self.rows[k] = (replace(d, user_id=user_id), h)
        return len(orphans)

    async def assign(self, device_id, user_id):
        entry = self.rows.get(device_id)
        if entry is None or entry[0].revoked_at is not None:
            return False
        self.rows[device_id] = (replace(entry[0], user_id=user_id), entry[1])
        return True


class _Pairing:
    def __init__(self) -> None:
        self.codes: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}

    async def purge(self, now):
        self.codes = {k: v for k, v in self.codes.items() if v["expires_at"] >= now}
        self.requests = {k: v for k, v in self.requests.items() if v["expires_at"] >= now}

    async def add_code(self, code, user_id, expires_at):
        if code in self.codes:
            return False
        self.codes[code] = {"user_id": user_id, "expires_at": expires_at, "attempts": 0, "used_at": None}
        return True

    async def discard_code(self, code):
        self.codes.pop(code, None)

    async def claim_code(self, code, now, max_attempts):
        c = self.codes.get(code)
        if c is None or c["used_at"] is not None or c["expires_at"] <= now or c["attempts"] >= max_attempts or c["user_id"] is None:
            return None
        c["used_at"] = now
        return c["user_id"]

    async def count_failed_attempt(self, now):
        for c in self.codes.values():
            if c["used_at"] is None and c["expires_at"] > now:
                c["attempts"] += 1

    async def add_request(self, poll_hash, request_id, device_name, now, expires_at):
        self.requests[poll_hash] = {
            "request_id": request_id, "device_name": device_name, "user_id": None, "status": "pending",
            "created_at": now, "expires_at": expires_at, "device_id": None,
        }

    def _record(self, poll_hash, r):
        return PairingRequest(poll_hash, r["request_id"], r["device_name"], r["user_id"], r["status"], r["expires_at"])

    def _pending(self, now):
        return [(h, r) for h, r in self.requests.items() if r["status"] == "pending" and r["expires_at"] > now]

    async def pending_ids(self, now):
        return {r["request_id"] for _, r in self._pending(now)}

    async def get_request(self, poll_hash, now):
        r = self.requests.get(poll_hash)
        return self._record(poll_hash, r) if r is not None and r["expires_at"] > now else None

    async def pending_by_id(self, request_id, now):
        return [self._record(h, r) for h, r in self._pending(now) if r["request_id"] == request_id]

    async def approve(self, poll_hash, user_id, now, expires_at):
        r = self.requests.get(poll_hash)
        if r is None or r["status"] != "pending" or r["expires_at"] <= now:
            return False
        r.update(status="approved", user_id=user_id, expires_at=expires_at)
        return True

    async def deliver(self, poll_hash):
        r = self.requests.get(poll_hash)
        if r is None or r["status"] != "approved":
            return False
        r["status"] = "delivered"
        return True

    async def set_request_device(self, poll_hash, device_id):
        if poll_hash in self.requests:
            self.requests[poll_hash]["device_id"] = device_id

    async def list_pending(self, now):
        return [self._record(h, r) for h, r in sorted(self._pending(now), key=lambda e: e[1]["created_at"])]


class _Agents:
    CALL_TYPES = {"conversation", "one-shot", "monologue"}

    def __init__(self) -> None:
        self.rows: dict[str, AgentRecord] = {}

    def _mine(self, user_id):
        return sorted((a for a in self.rows.values() if a.user_id == user_id), key=lambda a: (a.position, a.created_at))

    def _check(self, record, exclude=None):
        if record.call_type not in self.CALL_TYPES:
            raise ValueError(f"invalid call_type: {record.call_type}")
        if any(a.slug == record.slug and a.id != exclude for a in self._mine(record.user_id)):
            raise Conflict(f"agent slug already exists: {record.slug}")

    async def create(self, record, max_count=None):
        mine = self._mine(record.user_id)
        if max_count is not None and len(mine) >= max_count:
            raise LimitReached(f"agent limit reached ({max_count})")
        self._check(record)
        position = max((a.position for a in mine), default=-1) + 1
        self.rows[record.id] = replace(record, position=position)
        return self.rows[record.id]

    async def get(self, user_id, ref):
        return next((a for a in self._mine(user_id) if ref in (a.id, a.slug)), None)

    async def list(self, user_id):
        return self._mine(user_id)

    async def update(self, record):
        current = self.rows.get(record.id)
        if current is None or current.user_id != record.user_id:
            raise KeyError(record.id)
        self._check(record, exclude=record.id)
        self.rows[record.id] = replace(record, created_at=current.created_at, position=current.position)
        return self.rows[record.id]

    async def move(self, user_id, agent_id, index):
        ids = [a.id for a in self._mine(user_id)]
        if agent_id not in ids:
            return None
        ids.remove(agent_id)
        ids.insert(min(max(index, 0), len(ids)), agent_id)
        for position, each in enumerate(ids):
            self.rows[each] = replace(self.rows[each], position=position)
        return self.rows[agent_id]

    async def delete(self, user_id, agent_id):
        a = self.rows.get(agent_id)
        if a is None or a.user_id != user_id:
            return False
        del self.rows[agent_id]
        return True

    async def count(self, user_id):
        return len(self._mine(user_id))


class _Calls:
    def __init__(self) -> None:
        self.rows: dict[str, CallRecord] = {}

    async def create(self, record):
        self.rows[record.id] = record
        return record

    async def get(self, user_id, call_id):
        c = self.rows.get(call_id)
        return c if c and c.user_id == user_id else None

    async def save(self, record):
        current = self.rows[record.id]
        self.rows[record.id] = replace(
            record, user_id=current.user_id, agent_id=current.agent_id, device_id=current.device_id,
            call_type=current.call_type, created_at=current.created_at,
        )
        return self.rows[record.id]

    async def interrupt_unfinished(self, now):
        stale = [c for c in self.rows.values() if c.status in ("recording", "processing")]
        for c in stale:
            self.rows[c.id] = replace(c, status="failed", error="interrupted", finished_at=now, updated_at=now)
        return len(stale)


class _Meta:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key):
        return self.rows.get(key)

    async def set(self, key, value):
        self.rows[key] = value


class MemoryStorage:
    def __init__(self) -> None:
        self.users = _Users(self)
        self.tokens = _Tokens()
        self.devices = _Devices()
        self.pairing = _Pairing()
        self.agents = _Agents()
        self.calls = _Calls()
        self.meta = _Meta()

    def cascade(self, user_id: str) -> None:
        self.tokens.rows = {k: v for k, v in self.tokens.rows.items() if v[0].user_id != user_id}
        self.devices.rows = {k: v for k, v in self.devices.rows.items() if v[0].user_id != user_id}
        self.agents.rows = {k: v for k, v in self.agents.rows.items() if v.user_id != user_id}
        self.calls.rows = {k: v for k, v in self.calls.rows.items() if v.user_id != user_id}
        self.pairing.codes = {k: v for k, v in self.pairing.codes.items() if v["user_id"] != user_id}
        self.pairing.requests = {k: v for k, v in self.pairing.requests.items() if v["user_id"] != user_id}

    async def close(self) -> None:
        pass
