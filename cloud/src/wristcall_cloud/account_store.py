"""MongoDB storage for user accounts: identities, per-device sessions and the v2 migration.

`accounts` is the same collection `Store` uses (this module only adds optional fields to its documents).
Secrets (account key, refresh tokens, enroll codes, web session keys, invite codes) are stored as SHA-256 hex
digests only; nothing here puts one in a message.
"""

import hmac
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from .session_tokens import new_account_id, new_code, new_device_id, new_secret, secret_hash

SCHEMA_VERSION = 2
TOUCH_INTERVAL_S = 3600
_LEGACY_ID = re.compile(r"^(https?://[^#]+)#(.+)$", re.DOTALL)
_RACE_ATTEMPTS = 3
PURGE_AFTER_S = 3600  # an expired code or web session stays this long, then Mongo's TTL index drops it
PLANS = ("full", "custom")
_INVITE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"  # base32: 12 characters are 60 bits


class KeyInUse(Exception):
    """The account key hash already belongs to another account."""


class KeyExists(Exception):
    """The account already has a different account key."""


class DeviceLimit(Exception):
    """The account already has `max_devices` devices."""


class RefreshReused(Exception):
    """A superseded refresh token was presented: the device has been revoked."""


class WrongKey(Exception):
    """The current account key given does not match."""


class NoKey(Exception):
    """The account has no account key."""


class CodeLimit(Exception):
    """The account already has `max_open` open enroll codes."""


class AlreadyEntitled(Exception):
    """The account already has an entitlement that has not expired."""


def new_identity_id() -> str:
    return "idn_" + secrets.token_hex(8)


def new_invite_code() -> str:
    """`WC-XXXX-XXXX-XXXX`: 12 uppercase base32 characters (60 random bits)."""
    raw = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(12))
    return f"WC-{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def invite_hash(code: str) -> str:
    """The hash of an invite code as typed: case, spaces and hyphens do not count."""
    return secret_hash("".join(code.split()).replace("-", "").upper())


def _purge_at(expires_at: float) -> datetime:
    return datetime.fromtimestamp(expires_at + PURGE_AFTER_S, UTC)


def _check_plan(plan: str) -> None:
    if plan not in PLANS:
        raise ValueError(f"plan must be one of {', '.join(PLANS)}")


class AccountStore:
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
        self.accounts: AsyncCollection = db["accounts"]
        self.identities: AsyncCollection = db["identities"]
        self.devices: AsyncCollection = db["devices"]
        self.enroll_codes: AsyncCollection = db["enroll_codes"]
        self.web_sessions: AsyncCollection = db["web_sessions"]
        self.entitlements: AsyncCollection = db["entitlements"]
        self.invites: AsyncCollection = db["invites"]
        self.meta: AsyncCollection = db["meta"]

    async def ensure_indexes(self) -> None:
        await self.accounts.create_index(
            [("key_hash", ASCENDING)],
            unique=True,
            partialFilterExpression={"key_hash": {"$exists": True}},
            name="key_hash",
        )
        await self.accounts.create_index([("kind", ASCENDING), ("created_at", ASCENDING)], name="kind_created_at")
        await self.accounts.create_index([("last_seen_at", ASCENDING)], name="last_seen_at")
        await self.identities.create_index(
            [("kind", ASCENDING), ("issuer", ASCENDING), ("subject", ASCENDING)],
            unique=True,
            name="kind_issuer_subject",
        )
        await self.identities.create_index([("account", ASCENDING)], name="account")
        await self.devices.create_index([("account", ASCENDING)], name="account")
        await self.devices.create_index([("refresh_hash", ASCENDING)], unique=True, name="refresh_hash")
        await self.devices.create_index(
            [("prev_refresh_hash", ASCENDING)],
            partialFilterExpression={"prev_refresh_hash": {"$exists": True}},
            name="prev_refresh_hash",
        )
        await self.enroll_codes.create_index([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at")
        await self.enroll_codes.create_index([("account", ASCENDING)], name="account")
        await self.web_sessions.create_index([("purge_at", ASCENDING)], expireAfterSeconds=0, name="purge_at")
        await self.invites.create_index([("code_hash", ASCENDING)], unique=True, name="code_hash")

    async def migrate(self, *, now: float | None = None) -> int:
        """Gives every account named `<issuer>#<subject>` a `zitadel` identity; returns how many it created.

        Runs on every start (no version gate): idempotent, and it picks up accounts a rollback to 0.3.x created.
        Never changes `accounts` documents.
        """
        now = time.time() if now is None else now
        created = 0
        cursor = self.accounts.find({"_id": {"$regex": _LEGACY_ID.pattern}}, {"_id": 1})
        async for doc in cursor:
            match = _LEGACY_ID.match(doc["_id"])
            if match is None:
                continue
            issuer, subject = match.groups()
            result = await self.identities.update_one(
                {"kind": "zitadel", "issuer": issuer, "subject": subject},
                {"$setOnInsert": {"_id": new_identity_id(), "account": doc["_id"], "created_at": now}},
                upsert=True,
            )
            if result.upserted_id is not None:
                created += 1
        await self.meta.update_one(
            {"_id": "schema"}, {"$set": {"version": SCHEMA_VERSION, "migrated_at": now}}, upsert=True
        )
        return created

    # Accounts

    async def create_account(self, key_hash: str, *, now: float | None = None) -> dict[str, Any]:
        """Raises KeyInUse when the key hash belongs to an account already."""
        now = time.time() if now is None else now
        doc = {
            "_id": new_account_id(),
            "created_at": now,
            "last_seen_at": now,
            "key_hash": key_hash,
            "key_set_at": now,
            "kind": "anonymous",
        }
        try:
            await self.accounts.insert_one(doc)
        except DuplicateKeyError:
            raise KeyInUse("account key already in use") from None
        return doc

    async def account_by_key(self, key_hash: str) -> dict[str, Any] | None:
        return await self.accounts.find_one({"key_hash": key_hash})

    async def get_account(self, account: str) -> dict[str, Any] | None:
        return await self.accounts.find_one({"_id": account})

    async def touch(self, account: str, *, now: float | None = None) -> None:
        """Records `last_seen_at`, writing at most once an hour per account."""
        now = time.time() if now is None else now
        await self.accounts.update_one(_stale_filter(account, now), {"$set": {"last_seen_at": now}})

    async def set_key(self, account: str, key_hash: str, *, now: float | None = None) -> None:
        """Sets the account key when there is none; the same key is a no-op.

        Raises KeyExists (another key already set) or KeyInUse (the hash belongs to another account).
        A missing account is ignored (the caller has just authenticated it).
        """
        now = time.time() if now is None else now
        for _ in range(_RACE_ATTEMPTS):
            doc = await self.accounts.find_one({"_id": account}, {"key_hash": 1})
            if doc is None:
                return
            current = doc.get("key_hash")
            if current == key_hash:
                return
            if current is not None:
                raise KeyExists("account already has a key")
            try:
                result = await self.accounts.update_one(
                    {"_id": account, "key_hash": {"$exists": False}},
                    {"$set": {"key_hash": key_hash, "key_set_at": now}},
                )
            except DuplicateKeyError:
                raise KeyInUse("account key already in use") from None
            if result.matched_count == 1:
                return
        raise KeyExists("account already has a key")

    async def rotate_key(
        self, account: str, current_hash: str, new_hash: str, keep_device: str, *, now: float | None = None
    ) -> int:
        """Swaps the account key (only when `current_hash` matches) and drops everything the old key could reach.

        Deletes the other devices, the account's enroll codes and approved web sessions; returns how many devices
        went. Raises WrongKey, NoKey (the account has no key) or KeyInUse (`new_hash` is another account's).
        """
        now = time.time() if now is None else now
        try:
            swapped = await self.accounts.update_one(
                {"_id": account, "key_hash": current_hash}, {"$set": {"key_hash": new_hash, "key_set_at": now}}
            )
        except DuplicateKeyError:
            raise KeyInUse("account key already in use") from None
        if swapped.matched_count != 1:
            doc = await self.accounts.find_one({"_id": account}, {"key_hash": 1})
            if doc is None or doc.get("key_hash") is None:
                raise NoKey("account has no key")
            raise WrongKey("account key does not match")
        result = await self.devices.delete_many({"account": account, "_id": {"$ne": keep_device}})
        await self.enroll_codes.delete_many({"account": account})
        await self.web_sessions.delete_many({"account": account, "status": "approved"})
        return result.deleted_count

    async def count_created_since(self, since: float) -> int:
        """Anonymous accounts created at or after `since` (Unix seconds)."""
        return await self.accounts.count_documents({"kind": "anonymous", "created_at": {"$gte": since}})

    # Identities (one identity, one account; accounts are never merged)

    async def account_for_identity(self, kind: str, issuer: str, subject: str) -> str | None:
        doc = await self.identities.find_one({"kind": kind, "issuer": issuer, "subject": subject})
        return None if doc is None else doc["account"]

    async def add_identity(
        self, account: str, kind: str, issuer: str, subject: str, *, now: float | None = None
    ) -> str:
        """Links the identity to `account`; returns the owning account.

        When the identity already belongs to another account that one is returned and nothing moves.
        """
        now = time.time() if now is None else now
        for _ in range(_RACE_ATTEMPTS):
            doc = {
                "_id": new_identity_id(),
                "kind": kind,
                "issuer": issuer,
                "subject": subject,
                "account": account,
                "created_at": now,
            }
            try:
                await self.identities.insert_one(doc)
                return account
            except DuplicateKeyError:
                owner = await self.account_for_identity(kind, issuer, subject)
                if owner is not None:
                    return owner
        raise RuntimeError("could not link identity")

    async def identities_of(self, account: str) -> list[dict[str, Any]]:
        cursor = self.identities.find({"account": account}).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return await cursor.to_list(length=None)

    async def resolve_zitadel(self, issuer: str, subject: str, *, legacy_id: bool, now: float | None = None) -> str:
        """The account of a Zitadel identity, created on first use.

        An existing account named `<issuer>#<subject>` (not migrated yet) is adopted in both modes. Otherwise
        `legacy_id` True creates that very id (what 0.3.x did) and False creates an `acc_...` id. Two concurrent
        calls end on the same account: the loser of the identity insert deletes the account it just created.
        """
        now = time.time() if now is None else now
        legacy = f"{issuer}#{subject}"
        owner = await self.account_for_identity("zitadel", issuer, subject)
        if owner is not None:
            return owner
        created = False
        if await self.accounts.find_one({"_id": legacy}, {"_id": 1}) is not None:
            account = legacy
        elif legacy_id:
            account = legacy
            result = await self.accounts.update_one(
                {"_id": legacy},
                {"$setOnInsert": {"created_at": now, "last_seen_at": now, "kind": "zitadel"}},
                upsert=True,
            )
            created = result.upserted_id is not None
        else:
            account = new_account_id()
            await self.accounts.insert_one({"_id": account, "created_at": now, "last_seen_at": now, "kind": "zitadel"})
            created = True
        owner = await self.add_identity(account, "zitadel", issuer, subject, now=now)
        if owner != account and created:
            await self.accounts.delete_one({"_id": account})
        return owner

    # Devices

    async def add_device(
        self, account: str, device: dict[str, Any], *, max_devices: int, now: float | None = None
    ) -> tuple[dict[str, Any], str]:
        """Creates a device; returns it with its first refresh token. Raises DeviceLimit at `max_devices`."""
        now = time.time() if now is None else now
        # Count-then-insert can overshoot `max_devices` by one under concurrency (accepted: anti-abuse cap).
        if await self.devices.count_documents({"account": account}) >= max_devices:
            raise DeviceLimit("device limit reached")
        refresh = new_secret("wcr_")
        doc: dict[str, Any] = {
            "_id": new_device_id(),
            "account": account,
            "name": device["name"],
            "platform": device["platform"],
        }
        if device.get("public_key") is not None:
            doc["public_key"] = device["public_key"]
        doc |= {"created_at": now, "last_seen_at": now, "refresh_hash": secret_hash(refresh)}
        await self.devices.insert_one(doc)
        return doc, refresh

    async def count_devices(self, account: str) -> int:
        return await self.devices.count_documents({"account": account})

    async def device(self, account: str, device_id: str) -> dict[str, Any] | None:
        return await self.devices.find_one({"_id": device_id, "account": account})

    async def touch_device(self, device_id: str, *, now: float | None = None) -> None:
        """Records `last_seen_at`, writing at most once an hour per device."""
        now = time.time() if now is None else now
        await self.devices.update_one(_stale_filter(device_id, now), {"$set": {"last_seen_at": now}})

    async def rotate_refresh(
        self, refresh_token: str, *, idle_s: float, now: float | None = None
    ) -> tuple[dict[str, Any], str] | None:
        """Swaps a refresh token for a new one; returns (device, new token), or None when unknown or idle.

        Atomic: of two requests with the same token one wins, the other finds it superseded. Presenting a
        superseded token deletes that device and raises RefreshReused. An idle device (not seen for `idle_s`)
        answers None and is kept.
        """
        now = time.time() if now is None else now
        old_hash = secret_hash(refresh_token)
        new_token = new_secret("wcr_")
        doc = await self.devices.find_one_and_update(
            {"refresh_hash": old_hash, "last_seen_at": {"$gte": now - idle_s}},
            {"$set": {"refresh_hash": secret_hash(new_token), "prev_refresh_hash": old_hash, "last_seen_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            return doc, new_token
        reused = await self.devices.find_one_and_delete({"prev_refresh_hash": old_hash})
        if reused is not None:
            raise RefreshReused("refresh token already used")
        return None

    async def list_devices(self, account: str) -> list[dict[str, Any]]:
        cursor = self.devices.find({"account": account}).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return await cursor.to_list(length=None)

    async def delete_device(self, account: str, device_id: str) -> bool:
        result = await self.devices.delete_one({"_id": device_id, "account": account})
        return result.deleted_count == 1

    async def delete_account_data(self, account: str) -> None:
        """Deletes the account's devices, identities, enroll codes, web sessions and entitlement, then the account."""
        await self.devices.delete_many({"account": account})
        await self.identities.delete_many({"account": account})
        await self.enroll_codes.delete_many({"account": account})
        await self.web_sessions.delete_many({"account": account})
        await self.entitlements.delete_one({"_id": account})
        await self.accounts.delete_one({"_id": account})

    # Enroll codes (an app adds a watch: a code only ever creates a device of its `platforms`)

    async def add_enroll_code(
        self,
        account: str,
        *,
        ttl_s: int = 600,
        max_open: int,
        platforms: tuple[str, ...] = ("watchos",),
        now: float | None = None,
    ) -> tuple[str, float]:
        """A single-use code and its expiry. Raises CodeLimit at `max_open` unexpired codes."""
        now = time.time() if now is None else now
        # Count-then-insert can overshoot by one under concurrency (accepted: anti-abuse cap).
        if await self.enroll_codes.count_documents({"account": account, "expires_at": {"$gt": now}}) >= max_open:
            raise CodeLimit("enroll code limit reached")
        code = new_code("wce_")
        expires_at = now + ttl_s
        await self.enroll_codes.insert_one(
            {
                "_id": secret_hash(code),
                "account": account,
                "platforms": list(platforms),
                "created_at": now,
                "expires_at": expires_at,
                "purge_at": _purge_at(expires_at),
            }
        )
        return code, expires_at

    async def enroll_code(self, code: str, *, now: float | None = None) -> dict[str, Any] | None:
        """The unexpired code, read without spending it."""
        now = time.time() if now is None else now
        return await self.enroll_codes.find_one({"_id": secret_hash(code), "expires_at": {"$gt": now}})

    async def take_enroll_code(self, code: str, platform: str, *, now: float | None = None) -> str | None:
        """Spends the code when it is unexpired and allows `platform`; returns its account."""
        now = time.time() if now is None else now
        doc = await self.enroll_codes.find_one_and_delete(
            {"_id": secret_hash(code), "expires_at": {"$gt": now}, "platforms": platform}
        )
        return None if doc is None else doc["account"]

    # Web sessions (the web app shows a QR with the approve key; only the web app holds the poll key)

    async def add_web_session(
        self,
        device: dict[str, Any],
        user_agent: str,
        country: str | None,
        *,
        ttl_s: int = 300,
        now: float | None = None,
    ) -> dict[str, Any]:
        """A pending session; returns its id, both keys and expiry (the keys exist only in the return value)."""
        now = time.time() if now is None else now
        session_id = "ws_" + secrets.token_hex(8)
        approve_key, poll_key = new_secret("wck_"), new_secret("wcp_")
        expires_at = now + ttl_s
        await self.web_sessions.insert_one(
            {
                "_id": session_id,
                "approve_hash": secret_hash(approve_key),
                "poll_hash": secret_hash(poll_key),
                "device": device,
                "user_agent": user_agent,
                "country": country,
                "status": "pending",
                "created_at": now,
                "expires_at": expires_at,
                "purge_at": _purge_at(expires_at),
            }
        )
        return {"id": session_id, "approve_key": approve_key, "poll_key": poll_key, "expires_at": expires_at}

    async def _web_session(self, session_id: str, field: str, key: str, now: float) -> dict[str, Any] | None:
        """The unexpired session whose `field` hash is the hash of `key`."""
        doc = await self.web_sessions.find_one({"_id": session_id, "expires_at": {"$gt": now}})
        if doc is None or not hmac.compare_digest(doc[field], secret_hash(key)):
            return None
        return doc

    async def web_session(
        self, session_id: str, approve_key: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """The pending, unexpired session the approve key opens."""
        now = time.time() if now is None else now
        doc = await self._web_session(session_id, "approve_hash", approve_key, now)
        return doc if doc is not None and doc["status"] == "pending" else None

    async def approve_web_session(
        self, session_id: str, approve_key: str, account: str, *, now: float | None = None
    ) -> bool:
        """Approves a pending, unexpired session for `account`; False otherwise (approving twice included)."""
        now = time.time() if now is None else now
        doc = await self.web_session(session_id, approve_key, now=now)
        if doc is None:
            return False
        result = await self.web_sessions.update_one(
            {"_id": session_id, "status": "pending", "expires_at": {"$gt": now}},
            {"$set": {"status": "approved", "account": account, "approved_at": now}},
        )
        return result.modified_count == 1

    async def take_web_session(
        self, session_id: str, poll_key: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """A pending session as it is (kept); an approved one deleted and returned (once); None otherwise."""
        now = time.time() if now is None else now
        doc = await self._web_session(session_id, "poll_hash", poll_key, now)
        if doc is None or doc["status"] == "pending":
            return doc
        return await self.web_sessions.find_one_and_delete({"_id": session_id, "status": "approved"})

    # Invites and entitlements (one entitlement per account, `_id` = account)

    async def create_invites(self, plan: str, count: int, *, days: int, now: float | None = None) -> list[str]:
        """`count` single-use invite codes for `plan`, valid `days`; the codes exist only in the return value."""
        _check_plan(plan)
        if count < 1:
            raise ValueError("count must be at least 1")
        now = time.time() if now is None else now
        codes = [new_invite_code() for _ in range(count)]
        await self.invites.insert_many(
            [
                {
                    "_id": "inv_" + secrets.token_hex(8),
                    "code_hash": invite_hash(code),
                    "plan": plan,
                    "created_at": now,
                    "expires_at": now + days * 86400,
                    "used_by": None,
                    "used_at": None,
                }
                for code in codes
            ]
        )
        return codes

    async def redeem_invite(self, code: str, account: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Spends an unused, unexpired invite on `account`; returns the entitlement it grants (None: no such
        invite). Raises AlreadyEntitled, spending nothing, when the account has an unexpired entitlement."""
        now = time.time() if now is None else now
        # Two concurrent redeems by one account may both pass this check and spend two invites (accepted: the
        # later entitlement replaces the other; invites come from the admin).
        if await self.entitlement(account, now=now) is not None:
            raise AlreadyEntitled("account already has a plan")
        invite = await self.invites.find_one_and_update(
            {"code_hash": invite_hash(code), "used_by": None, "expires_at": {"$gt": now}},
            {"$set": {"used_by": account, "used_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if invite is None:
            return None
        doc = {
            "_id": account,
            "plan": invite["plan"],
            "source": "beta",
            "invite": invite["_id"],
            "granted_at": now,
            "expires_at": None,
        }
        await self.entitlements.replace_one({"_id": account}, doc, upsert=True)
        return doc

    async def grant(
        self, account: str, plan: str, *, days: int | None, source: str = "beta", now: float | None = None
    ) -> dict[str, Any]:
        """Sets the account's entitlement (replacing any): `days` long, or without end when None."""
        _check_plan(plan)
        now = time.time() if now is None else now
        doc = {
            "_id": account,
            "plan": plan,
            "source": source,
            "granted_at": now,
            "expires_at": None if days is None else now + days * 86400,
        }
        await self.entitlements.replace_one({"_id": account}, doc, upsert=True)
        return doc

    async def revoke_entitlement(self, account: str) -> bool:
        result = await self.entitlements.delete_one({"_id": account})
        return result.deleted_count == 1

    async def entitlement(self, account: str, *, now: float | None = None) -> dict[str, Any] | None:
        """The account's entitlement while it has not expired."""
        now = time.time() if now is None else now
        doc = await self.entitlements.find_one({"_id": account})
        if doc is None:
            return None
        expires_at = doc.get("expires_at")
        if expires_at is not None and expires_at <= now:
            return None
        return doc


def _stale_filter(doc_id: str, now: float) -> dict[str, Any]:
    return {
        "_id": doc_id,
        "$or": [{"last_seen_at": {"$lt": now - TOUCH_INTERVAL_S}}, {"last_seen_at": {"$exists": False}}],
    }
