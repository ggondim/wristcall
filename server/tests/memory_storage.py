"""Storage made of dicts: runs the contract tests (test_storage.py) next to the SQLite adapter.

It proves the interface in wristcall/storage/base.py needs no SQL, which the Cloud API adapter of
epic E9 relies on. Not shipped: it lives in tests/.
"""

from dataclasses import replace

from wristcall.storage import AgentRecord, ApiToken, CallRecord, Conflict, Device, EntryRecord, LimitReached, PairingRequest, User

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

    async def link_central(self, user_id, subject):
        user = self.rows.get(user_id)
        if user is None:
            return False
        if any(u.central_subject == subject and u.id != user_id for u in self.rows.values()):
            raise Conflict("central account already linked to another user")
        self.rows[user_id] = replace(user, central_subject=subject)
        return True

    async def unlink_central(self, user_id):
        user = self.rows.get(user_id)
        if user is None or user.central_subject is None:
            return False
        self.rows[user_id] = replace(user, central_subject=None)
        return True

    async def by_central(self, subject):
        return next((u for u in self.rows.values() if u.central_subject == subject), None)


class _Tokens:
    def __init__(self, push: "_Push") -> None:
        self._push = push
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
        self._push.rows = [r for r in self._push.rows if r["token_id"] != token_id]
        return True


class _Devices:
    def __init__(self, push: "_Push") -> None:
        self._push = push
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
        self._push.rows = [r for r in self._push.rows if r["device_id"] != device_id]
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

    async def add_request(self, poll_hash, request_id, device_name, now, expires_at, target_user_id=None):
        self.requests[poll_hash] = {
            "request_id": request_id, "device_name": device_name, "user_id": None, "status": "pending",
            "created_at": now, "expires_at": expires_at, "device_id": None, "target_user_id": target_user_id,
        }

    def _record(self, poll_hash, r):
        return PairingRequest(
            poll_hash, r["request_id"], r["device_name"], r["user_id"], r["status"], r["expires_at"], r["target_user_id"]
        )

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

    async def deny(self, poll_hash):
        r = self.requests.get(poll_hash)
        if r is None or r["status"] != "pending":
            return False
        r["status"] = "denied"
        return True

    async def pending_for(self, target_user_id, now):
        found = [(h, r) for h, r in self._pending(now) if r["target_user_id"] == target_user_id]
        return [self._record(h, r) for h, r in sorted(found, key=lambda e: e[1]["created_at"])]

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
        self.entry_rows: dict[str, list[tuple[EntryRecord, set[str]]]] = {}

    async def create(self, record):
        self.rows[record.id] = record
        return record

    async def get(self, user_id, call_id):
        c = self.rows.get(call_id)
        return c if c and c.user_id == user_id else None

    async def save(self, record):
        current = self.rows[record.id]
        self.rows[record.id] = replace(
            current, status=record.status, error=record.error, attempts=record.attempts,
            last_http_status=record.last_http_status, ended_at=record.ended_at, finished_at=record.finished_at,
            updated_at=record.updated_at,
        )
        return self.rows[record.id]

    async def interrupt_unfinished(self, now):
        stale = [c for c in self.rows.values() if c.status in ("recording", "processing")]
        for c in stale:
            status = "ended" if c.call_type == "conversation" else "failed"
            self.rows[c.id] = replace(
                c, status=status, error="interrupted", ended_at=c.ended_at or now, finished_at=now, updated_at=now,
            )
        return len(stale)

    async def add_entry(self, user_id, entry, terms):
        if await self.get(user_id, entry.call_id) is None:
            return False
        self.entry_rows.setdefault(entry.call_id, []).append((entry, set(terms)))
        return True

    async def entries(self, user_id, call_id):
        if await self.get(user_id, call_id) is None:
            return []
        return sorted((e for e, _ in self.entry_rows.get(call_id, [])), key=lambda e: e.seq)

    def _matches(self, call_id, terms):
        rows = self.entry_rows.get(call_id, [])
        return all(any(set(group) & found for _, found in rows) for group in terms)

    async def list(self, user_id, *, agent_id=None, since=None, until=None, terms=None, before=None, limit=50):
        if terms is not None and not terms:
            return []
        calls = sorted(
            (c for c in self.rows.values() if c.user_id == user_id), key=lambda c: (c.created_at, c.id), reverse=True,
        )
        if before is not None:
            calls = [c for c in calls if (c.created_at, c.id) < tuple(before)]
        return [
            c for c in calls
            if (agent_id is None or c.agent_id == agent_id)
            and (since is None or c.created_at >= since)
            and (until is None or c.created_at < until)
            and (not terms or self._matches(c.id, terms))
        ][:limit]

    def _drop(self, call_ids):
        for call_id in call_ids:
            self.rows.pop(call_id, None)
            self.entry_rows.pop(call_id, None)
        return len(call_ids)

    async def delete(self, user_id, call_id):
        return self._drop([call_id] if await self.get(user_id, call_id) else []) == 1

    async def delete_all(self, user_id, agent_id=None):
        return self._drop([
            c.id for c in self.rows.values() if c.user_id == user_id and (agent_id is None or c.agent_id == agent_id)
        ])

    async def set_expiry(self, user_id, agent_id, retention_s):
        mine = [c for c in self.rows.values() if c.user_id == user_id and c.agent_id == agent_id]
        for c in mine:
            self.rows[c.id] = replace(c, expires_at=None if retention_s is None else c.created_at + retention_s)
        return len(mine)

    async def cap_expiry(self, max_retention_s):
        longer = [
            c for c in self.rows.values() if c.expires_at is None or c.expires_at > c.created_at + max_retention_s
        ]
        for c in longer:
            self.rows[c.id] = replace(c, expires_at=c.created_at + max_retention_s)
        return len(longer)

    async def entries_by_seal(self, sealed, limit):
        found = [e for rows in self.entry_rows.values() for e, _ in rows if e.text is not None and e.sealed == sealed]
        return found[:limit]

    async def replace_entry(self, entry, terms):
        rows = self.entry_rows.get(entry.call_id, [])
        for i, (e, _) in enumerate(rows):
            if e.seq == entry.seq:
                rows[i] = (replace(e, text=entry.text, sealed=entry.sealed), set(terms))
                return True
        return False

    async def purge_expired(self, now, limit=500):
        return self._drop([
            c.id for c in self.rows.values()
            if c.expires_at is not None and c.expires_at <= now and c.status not in ("recording", "processing")
        ][:limit])

    async def compact(self):
        pass


def _one_client(device_id, token_id):
    if (device_id is None) == (token_id is None):
        raise ValueError("exactly one of device_id and token_id")


class _Push:
    def __init__(self, root: "MemoryStorage") -> None:
        self._root = root
        self.rows: list[dict] = []  # user_id, device_id, token_id, push_key, created_at (insertion order)

    def _find(self, device_id, token_id):
        return next((r for r in self.rows if r["device_id"] == device_id and r["token_id"] == token_id), None)

    async def set(self, user_id, push_key, now, *, device_id=None, token_id=None):
        _one_client(device_id, token_id)
        row = self._find(device_id, token_id)
        if row is None:
            self.rows.append(dict(user_id=user_id, device_id=device_id, token_id=token_id, push_key=push_key, created_at=now))
            return None
        if row["push_key"] == push_key:
            return None
        old = row["push_key"]
        row.update(push_key=push_key, user_id=user_id)
        return old

    async def clear(self, *, device_id=None, token_id=None):
        _one_client(device_id, token_id)
        row = self._find(device_id, token_id)
        if row is None:
            return None
        self.rows.remove(row)
        return row["push_key"]

    async def forget(self, push_key):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["push_key"] != push_key]
        return before - len(self.rows)

    async def for_device(self, device_id):
        entry = self._root.devices.rows.get(device_id)
        if entry is None or entry[0].revoked_at is not None:
            return None
        row = self._find(device_id, None)
        return row["push_key"] if row else None

    async def for_apps(self, user_id):
        found = [
            r for r in self.rows
            if r["user_id"] == user_id and r["token_id"] in self._root.tokens.rows
            and self._root.tokens.rows[r["token_id"]][0].revoked_at is None
        ]
        return [r["push_key"] for r in sorted(found, key=lambda r: r["created_at"])]  # stable: ties keep insertion order


class _Meta:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key):
        return self.rows.get(key)

    async def set(self, key, value):
        self.rows[key] = value

    async def delete(self, key):
        self.rows.pop(key, None)


class MemoryStorage:
    def __init__(self) -> None:
        self.users = _Users(self)
        self.push = _Push(self)
        self.tokens = _Tokens(self.push)
        self.devices = _Devices(self.push)
        self.pairing = _Pairing()
        self.agents = _Agents()
        self.calls = _Calls()
        self.meta = _Meta()

    def cascade(self, user_id: str) -> None:
        self.push.rows = [r for r in self.push.rows if r["user_id"] != user_id]
        self.tokens.rows = {k: v for k, v in self.tokens.rows.items() if v[0].user_id != user_id}
        self.devices.rows = {k: v for k, v in self.devices.rows.items() if v[0].user_id != user_id}
        self.agents.rows = {k: v for k, v in self.agents.rows.items() if v.user_id != user_id}
        self.calls._drop([c.id for c in self.calls.rows.values() if c.user_id == user_id])
        self.pairing.codes = {k: v for k, v in self.pairing.codes.items() if v["user_id"] != user_id}
        self.pairing.requests = {
            k: v for k, v in self.pairing.requests.items() if user_id not in (v["user_id"], v["target_user_id"])
        }

    async def close(self) -> None:
        pass
