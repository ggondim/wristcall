"""The server's Storage over the Wristcall Cloud's node API (`/v1/node/*`), for cloud mode: nothing on disk.

Every operation acts for the account of the request being served: it first checks the request's account proof
(`cloud_node.proof.require_proof`, before any trip to the Cloud) and sends the user's per-server token with it, so the
Cloud decides what that device may do. Records always carry the proof's account, never a field the Cloud sent.

- agents: kept in the Cloud with each user endpoint of the `spec` sealed (`SealCodec`); the listing is cached per
  account for `agents_ttl_s` and then revalidated with `If-None-Match` (a `304` keeps it); every write drops the
  account's cache. An agent whose `spec` does not open (another account, a clear-text endpoint, damage) or that the
  server would refuse is logged by id and left out: it is not listed and `get` answers None.
- calls: read, searched, deleted and exported; written only by the call path (`CloudCallLog` and the queue, with the
  call grant), so `create`, `add_entry` and `save` raise `NotSupported`, as do the operator methods. `set_expiry` is a
  no-op: the Cloud moves the expiry itself when an agent's `spec` changes. A call read with `get` is kept for a few
  seconds in a memo of the request (a `ContextVar`), so the `entries` right after it costs no second trip; a history
  page of 50 calls still costs up to 51 trips (the listing plus one per call: the Cloud lists no entries).
- push: the Cloud keeps one target per device, the device of the request's token.
- users: virtual (the account itself); tokens, devices and pairing do not exist in cloud mode (`NotSupported`).
- meta: a dictionary in memory.

Errors: `ProofMismatch` (a `PermissionError`) without a proof for the account; `PlanRequired`, `DeviceRevoked` and
`CloudUnavailable` (also for an unexpected status or an answer without the expected fields) from any operation; the
specific 4xx of each route as the Storage protocol has them (`Conflict`, `LimitReached`, `KeyError`, `CallOpen`), and
`ValueError` for a `422 invalid`.
"""

from __future__ import annotations  # the stores have methods named `list`: no annotation is evaluated in their scope

import copy
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NoReturn
from urllib.parse import quote

from pydantic import ValidationError

from ..cloud_node.client import CloudClient, CloudResponse, raise_for_account
from ..cloud_node.proof import ProofMismatch, UserProof, current_proof, require_proof
from ..cloud_node.seal import SealCodec, SealError
from .models import (
    AgentRecord,
    CallOpen,
    CallRecord,
    CloudUnavailable,
    Conflict,
    EntryRecord,
    LimitReached,
    NotSupported,
    User,
)

log = logging.getLogger("wristcall.storage.cloud")

# What may travel in a path: agent ids (`ag_` + hex) and slugs, call ids (`c_` + hex). Anything else is no record.
PATH_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TERM_HASH = re.compile(r"^x[0-9a-f]{16}$")  # the only search terms the Cloud keeps (history_codec's hashes)
MAX_GROUPS, MAX_GROUP_TERMS = 16, 16  # the Cloud's limits on a search
MAX_LIMIT = 200  # the Cloud's page ceiling
MAX_INDEX = 1_000_000  # an index past the end means last: clamped before it travels
AGENT_PATCH_FIELDS = ("slug", "display_name", "icon", "color", "call_type", "spec")
CALL_MEMO_TTL_S = 5.0
CALL_MEMO_SIZE = 64
AGENTS_CACHE_SIZE = 1024


@dataclass
class _Listing:
    fetched_at: float
    etag: str
    agents: list[dict[str, Any]]  # as the Cloud sent them (spec sealed)


@dataclass
class _CallMemo:
    proof: UserProof  # the request it belongs to: another proof (another request) starts an empty memo
    calls: dict[str, tuple[float, dict[str, Any]]]  # call id -> (when, the Cloud's {"call", "entries"})


_call_memo: ContextVar[_CallMemo | None] = ContextVar("wristcall_cloud_call_memo", default=None)


def _unsupported(name: str) -> Callable[..., Any]:
    async def method(self: Any, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotSupported(f"{name} is not available in cloud mode")

    method.__name__ = name.rsplit(".", 1)[-1]
    method.__qualname__ = name
    return method


def unexpected_answer(resp: CloudResponse, what: str) -> NoReturn:
    log.warning("Wristcall Cloud answered %d (%s) on %s", resp.status, resp.code, what)
    raise CloudUnavailable(f"Wristcall Cloud answered {resp.status} on {what}")


@contextmanager
def answer_shape(what: str) -> Iterator[None]:
    """An answer without the fields it should have is no usable answer."""
    try:
        yield
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        log.warning("Wristcall Cloud answered %s without the expected fields", what)
        raise CloudUnavailable(f"Wristcall Cloud answered {what} without the expected fields") from None


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("not a string")
    return value


def _opt_str(value: Any) -> str | None:
    return None if value is None else _str(value)


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("not a number")
    return float(value)


def _opt_num(value: Any) -> float | None:
    return None if value is None else _num(value)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("not an integer")
    return value


def _opt_int(value: Any) -> int | None:
    return None if value is None else _int(value)


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("not a list")
    return value


def call_record(account: str, c: Any) -> CallRecord:
    return CallRecord(
        id=_str(c["id"]), user_id=account, agent_id=_str(c["agent_id"]), device_id=_opt_str(c["device_id"]),
        call_type=_str(c["call_type"]), status=_str(c["status"]), created_at=_num(c["created_at"]),
        updated_at=_num(c["updated_at"]), error=_opt_str(c["error"]), attempts=_int(c["attempts"]),
        last_http_status=_opt_int(c["last_http_status"]), ended_at=_opt_num(c["ended_at"]),
        finished_at=_opt_num(c["finished_at"]), agent_slug=_str(c["agent_slug"]), agent_name=_str(c["agent_name"]),
        expires_at=_opt_num(c["expires_at"]), delivery=_opt_str(c["delivery"]),
    )


def _entry_record(call_id: str, e: Any) -> EntryRecord:
    sealed = e["sealed"]
    if not isinstance(sealed, bool):
        raise TypeError("sealed is not a boolean")
    return EntryRecord(call_id=call_id, seq=_int(e["seq"]), role=_str(e["role"]), text=_opt_str(e["text"]),
                       sealed=sealed, error=_opt_str(e["error"]), at=_num(e["at"]))


def entry_records(call_id: str, raw: Any) -> list[EntryRecord]:
    return sorted((_entry_record(call_id, e) for e in _list(raw)), key=lambda e: e.seq)


def _params(**fields: Any) -> list[tuple[str, str]]:
    """Query pairs, skipping None; numbers as Python writes them (the Cloud reads any float)."""
    return [(k, repr(float(v)) if isinstance(v, float) else str(v)) for k, v in fields.items() if v is not None]


class CloudStorage:
    def __init__(
        self, client: CloudClient, seal: SealCodec, *, now: Callable[[], float] = time.time, agents_ttl_s: float = 30.0,
    ) -> None:
        self.users = _Users()
        self.tokens = _Tokens()
        self.devices = _Devices()
        self.pairing = _Pairing()
        self.agents = _Agents(client, seal, now, agents_ttl_s)
        self.calls = _Calls(client, now)
        self.push = _Push(client)
        self.meta = _Meta()

    async def close(self) -> None:
        """The HTTP client belongs to whoever made it (`create_app`); only the cache goes."""
        self.agents.forget_all()


async def _account_request(
    client: CloudClient, method: str, path: str, proof: UserProof, **kwargs: Any,
) -> CloudResponse:
    """One request for the proof's account; the answers every account route shares raise first."""
    resp = await client.request(method, path, user=proof, **kwargs)
    raise_for_account(resp)
    return resp


class _Users:
    """The account is the user: nothing is stored on the node."""

    async def get(self, user_id: str) -> User | None:
        require_proof(user_id)
        return User(id=user_id, handle=user_id, display_name="", created_at=0.0)

    create = _unsupported("users.create")
    by_handle = _unsupported("users.by_handle")
    list = _unsupported("users.list")
    update = _unsupported("users.update")
    delete = _unsupported("users.delete")
    link_central = _unsupported("users.link_central")
    unlink_central = _unsupported("users.unlink_central")
    by_central = _unsupported("users.by_central")


class _Tokens:
    create = _unsupported("tokens.create")
    authenticate = _unsupported("tokens.authenticate")
    list = _unsupported("tokens.list")
    revoke = _unsupported("tokens.revoke")


class _Devices:
    create = _unsupported("devices.create")
    by_token = _unsupported("devices.by_token")
    list = _unsupported("devices.list")
    count = _unsupported("devices.count")
    revoke = _unsupported("devices.revoke")
    adopt_orphans = _unsupported("devices.adopt_orphans")
    assign = _unsupported("devices.assign")


class _Pairing:
    purge = _unsupported("pairing.purge")
    add_code = _unsupported("pairing.add_code")
    discard_code = _unsupported("pairing.discard_code")
    claim_code = _unsupported("pairing.claim_code")
    count_failed_attempt = _unsupported("pairing.count_failed_attempt")
    add_link_code = _unsupported("pairing.add_link_code")
    claim_link_code = _unsupported("pairing.claim_link_code")
    discard_link_codes = _unsupported("pairing.discard_link_codes")
    add_request = _unsupported("pairing.add_request")
    pending_ids = _unsupported("pairing.pending_ids")
    get_request = _unsupported("pairing.get_request")
    pending_by_id = _unsupported("pairing.pending_by_id")
    approve = _unsupported("pairing.approve")
    deliver = _unsupported("pairing.deliver")
    deny = _unsupported("pairing.deny")
    pending_for = _unsupported("pairing.pending_for")
    set_request_device = _unsupported("pairing.set_request_device")
    list_pending = _unsupported("pairing.list_pending")


class _Agents:
    def __init__(self, client: CloudClient, seal: SealCodec, now: Callable[[], float], ttl_s: float) -> None:
        self._client, self._seal, self._now, self._ttl_s = client, seal, now, ttl_s
        self._cache: dict[str, _Listing] = {}  # account -> its last listing
        # Writes so far, of any account, only ever growing: a listing that was in flight while a write landed is not
        # cached (it may predate it). One counter for all accounts, so no reset can make an old count come back.
        self._writes = 0

    def forget_all(self) -> None:
        self._cache.clear()

    def _record(self, account: str, raw: Any) -> AgentRecord | None:
        """The agent as the server uses it, its spec opened with the proof's account; None (logged by id) when the
        spec does not open or the server would refuse it. A raw agent without its fields raises (shape)."""
        from ..agents import AgentSpec  # agents imports the storage package: a top-level import would be circular

        agent_id = _str(raw["id"])
        fields = dict(
            id=agent_id, user_id=account, slug=_str(raw["slug"]), display_name=_str(raw["display_name"]),
            icon=_str(raw["icon"]), call_type=_str(raw["call_type"]), position=_int(raw["position"]),
            created_at=_num(raw["created_at"]), updated_at=_num(raw["updated_at"]), color=_opt_str(raw["color"]),
        )
        spec = raw["spec"]
        try:
            opened = self._seal.open_spec(copy.deepcopy(spec), account, agent_id)
            AgentSpec.model_validate(opened)
        except (SealError, ValidationError) as e:  # their text may quote the spec: only the kind is logged
            log.warning("agent %s left out: its spec cannot be used (%s)", agent_id, type(e).__name__)
            return None
        return AgentRecord(**fields, spec=opened)

    def _records(self, account: str, raws: list[dict[str, Any]]) -> list[AgentRecord]:
        with answer_shape("agents"):
            found = [self._record(account, raw) for raw in raws]
        return [r for r in found if r is not None]

    def opened(self, account: str, raws: list[Any]) -> list[AgentRecord]:
        """Agents as the Cloud sent them elsewhere (`GET /v1/node/me`), opened as `list` opens them. Raises
        `CloudUnavailable` when one is not an object or lacks its fields."""
        if not all(isinstance(a, dict) for a in raws):
            log.warning("Wristcall Cloud answered agents that are not objects")
            raise CloudUnavailable("Wristcall Cloud answered agents that are not objects")
        return self._records(account, raws)

    async def _listing(self, proof: UserProof, *, revalidate: bool = False) -> list[dict[str, Any]]:
        cached = self._cache.get(proof.account)
        now = self._now()
        if cached is not None and not revalidate and now - cached.fetched_at < self._ttl_s:
            return cached.agents
        headers = {"If-None-Match": cached.etag} if cached is not None else None
        writes = self._writes
        resp = await _account_request(self._client, "GET", "/v1/node/agents", proof, headers=headers)
        if resp.status == 304 and cached is not None:
            if self._writes == writes:
                cached.fetched_at = now
            return cached.agents
        if resp.status != 200:
            unexpected_answer(resp, "GET /v1/node/agents")
        with answer_shape("GET /v1/node/agents"):
            agents = _list(resp.body["agents"])
            if not all(isinstance(a, dict) for a in agents):
                raise TypeError("an agent is not an object")
            etag = _str(resp.body["etag"])
        if self._writes == writes:
            self._cache.pop(proof.account, None)
            if len(self._cache) >= AGENTS_CACHE_SIZE:
                self._cache.pop(next(iter(self._cache)))  # the account cached longest ago
            self._cache[proof.account] = _Listing(now, etag, agents)
        return agents

    def _changed(self, account: str) -> None:
        self._cache.pop(account, None)
        self._writes += 1

    async def list(self, user_id: str) -> list[AgentRecord]:
        proof = require_proof(user_id)
        return self._records(proof.account, await self._listing(proof))

    async def count(self, user_id: str) -> int:
        return len(await self.list(user_id))

    async def get(self, user_id: str, ref: str) -> AgentRecord | None:
        proof = require_proof(user_id)
        if not PATH_REF.fullmatch(ref):
            return None
        path = f"/v1/node/agents/{quote(ref, safe='')}"
        resp = await _account_request(self._client, "GET", path, proof)
        if resp.status == 404:
            return None
        if resp.status != 200:
            unexpected_answer(resp, "GET /v1/node/agents/{ref}")
        with answer_shape("GET /v1/node/agents/{ref}"):
            return self._record(proof.account, resp.body)

    def _written(self, account: str, resp: CloudResponse, what: str) -> AgentRecord:
        with answer_shape(what):
            record = self._record(account, resp.body)
        if record is None:  # what this node just sealed does not open: the Cloud changed it
            raise CloudUnavailable(f"Wristcall Cloud answered {what} with an agent that does not open")
        return record

    async def create(self, record: AgentRecord, max_count: int | None = None) -> AgentRecord:
        proof = require_proof(record.user_id)
        if max_count is not None:  # checked on a fresh listing; the Cloud also holds its own ceiling
            current = self._records(proof.account, await self._listing(proof, revalidate=True))
            if len(current) >= max_count:
                raise LimitReached(f"at most {max_count} agents")
        body = {
            "id": record.id, "slug": record.slug, "display_name": record.display_name, "icon": record.icon,
            "call_type": record.call_type, "color": record.color,
            "spec": self._seal.seal_spec(record.spec, proof.account, record.id),
        }
        try:
            resp = await _account_request(self._client, "POST", "/v1/node/agents", proof, json=body)
        finally:
            self._changed(proof.account)
        if resp.status == 409:
            raise Conflict("an agent with this id or slug exists")
        if resp.status == 403 and resp.code == "limit":
            raise LimitReached("the Cloud's agent limit")
        if resp.status == 422:
            raise ValueError("the Cloud refused the agent (invalid)")
        if resp.status not in (200, 201):
            unexpected_answer(resp, "POST /v1/node/agents")
        return self._written(proof.account, resp, "POST /v1/node/agents")

    async def update(self, record: AgentRecord) -> AgentRecord:
        proof = require_proof(record.user_id)
        if not PATH_REF.fullmatch(record.id):
            raise KeyError(record.id)
        sealed = self._seal.seal_spec(record.spec, proof.account, record.id)
        fields = {"slug": record.slug, "display_name": record.display_name, "icon": record.icon,
                  "color": record.color, "call_type": record.call_type, "spec": sealed}
        body = {k: fields[k] for k in AGENT_PATCH_FIELDS}
        try:
            resp = await _account_request(
                self._client, "PATCH", f"/v1/node/agents/{quote(record.id, safe='')}", proof, json=body,
            )
        finally:
            self._changed(proof.account)
        if resp.status == 404:
            raise KeyError(record.id)
        if resp.status == 409:
            raise Conflict("an agent with this slug exists")
        if resp.status == 422:
            raise ValueError("the Cloud refused the agent (invalid)")
        if resp.status != 200:
            unexpected_answer(resp, "PATCH /v1/node/agents/{id}")
        return self._written(proof.account, resp, "PATCH /v1/node/agents/{id}")

    async def move(self, user_id: str, agent_id: str, index: int) -> AgentRecord | None:
        proof = require_proof(user_id)
        body = {"agent": agent_id, "index": max(0, min(index, MAX_INDEX))}
        try:
            resp = await _account_request(self._client, "PUT", "/v1/node/agents/order", proof, json=body)
        finally:
            self._changed(proof.account)
        if resp.status == 404:
            return None
        if resp.status != 200:
            unexpected_answer(resp, "PUT /v1/node/agents/order")
        with answer_shape("PUT /v1/node/agents/order"):
            moved = next((a for a in _list(resp.body["agents"]) if a["id"] == agent_id), None)
            if moved is None:
                raise KeyError(agent_id)
            return self._record(proof.account, moved)

    async def delete(self, user_id: str, agent_id: str) -> bool:
        proof = require_proof(user_id)
        if not PATH_REF.fullmatch(agent_id):
            return False
        try:
            resp = await _account_request(self._client, "DELETE", f"/v1/node/agents/{quote(agent_id, safe='')}", proof)
        finally:
            self._changed(proof.account)
        if resp.status == 404:
            return False
        if resp.status not in (200, 204):
            unexpected_answer(resp, "DELETE /v1/node/agents/{id}")
        return True


class _Calls:
    # Capability (CallStore.exports): `export` streams every matching call with its entries in one Cloud request.
    exports = True

    def __init__(self, client: CloudClient, now: Callable[[], float]) -> None:
        self._client, self._now = client, now

    def _memo(self, proof: UserProof) -> dict[str, tuple[float, dict[str, Any]]]:
        memo = _call_memo.get()
        if memo is None or memo.proof is not proof:
            memo = _CallMemo(proof, {})
            _call_memo.set(memo)
        return memo.calls

    async def _read(self, proof: UserProof, call_id: str, *, keep: bool) -> dict[str, Any] | None:
        """`{"call", "entries"}` of one call, None if the account has no such call. `keep` leaves it in the request's
        memo for the next read (a `get` followed by `entries`); a read from the memo takes it out."""
        memo = self._memo(proof)
        hit = memo.pop(call_id, None)
        if hit is not None and self._now() - hit[0] < CALL_MEMO_TTL_S:
            if keep:
                memo[call_id] = hit
            return hit[1]
        if not PATH_REF.fullmatch(call_id):
            return None
        resp = await _account_request(self._client, "GET", f"/v1/node/calls/{quote(call_id, safe='')}", proof)
        if resp.status == 404:
            return None
        if resp.status != 200:
            unexpected_answer(resp, "GET /v1/node/calls/{id}")
        with answer_shape("GET /v1/node/calls/{id}"):
            found = {"call": call_record(proof.account, resp.body["call"]),
                     "entries": entry_records(call_id, resp.body["entries"])}
        if keep:
            while len(memo) >= CALL_MEMO_SIZE:
                memo.pop(next(iter(memo)))
            memo[call_id] = (self._now(), found)
        return found

    async def get(self, user_id: str, call_id: str) -> CallRecord | None:
        found = await self._read(require_proof(user_id), call_id, keep=True)
        return None if found is None else found["call"]

    async def entries(self, user_id: str, call_id: str) -> list[EntryRecord]:
        found = await self._read(require_proof(user_id), call_id, keep=False)
        return [] if found is None else list(found["entries"])

    async def list(
        self, user_id: str, *, agent_id: str | None = None, since: float | None = None, until: float | None = None,
        terms: list[list[str]] | None = None, before: tuple[float, str] | None = None, limit: int = 50,
    ) -> list[CallRecord]:
        proof = require_proof(user_id)
        groups: list[list[str]] = []
        if terms is not None:
            # The Cloud keeps only the hashed terms: a plain word (kept for entries written before the key) is dropped,
            # and a group left empty matches nothing, as `[]` does.
            groups = [list(dict.fromkeys(t for t in group if TERM_HASH.fullmatch(t))) for group in terms]
            if not groups or not all(groups):
                return []
            if len(groups) > MAX_GROUPS or any(len(g) > MAX_GROUP_TERMS for g in groups):
                raise ValueError(f"at most {MAX_GROUPS} groups of {MAX_GROUP_TERMS} search terms")
        params = _params(agent=agent_id, since=since, until=until)
        if before is not None:
            params += _params(before_at=float(before[0]), before_id=before[1])
        params += [("terms", ",".join(g)) for g in groups]
        params += _params(limit=max(1, min(limit, MAX_LIMIT)))
        resp = await _account_request(self._client, "GET", "/v1/node/calls", proof, params=params)
        if resp.status != 200:
            unexpected_answer(resp, "GET /v1/node/calls")
        with answer_shape("GET /v1/node/calls"):
            return [call_record(proof.account, c) for c in _list(resp.body["calls"])]

    async def delete(self, user_id: str, call_id: str) -> bool:
        proof = require_proof(user_id)
        self._memo(proof).pop(call_id, None)
        if not PATH_REF.fullmatch(call_id):
            return False
        resp = await _account_request(self._client, "DELETE", f"/v1/node/calls/{quote(call_id, safe='')}", proof)
        if resp.status == 404:
            return False
        if resp.status == 409 and resp.code == "call_open":
            raise CallOpen("this call is still open")
        if resp.status not in (200, 204):
            unexpected_answer(resp, "DELETE /v1/node/calls/{id}")
        return True

    async def delete_all(self, user_id: str, agent_id: str | None = None) -> int:
        proof = require_proof(user_id)
        self._memo(proof).clear()
        params = [("agent", agent_id)] if agent_id is not None else [("all", "true")]
        resp = await _account_request(self._client, "DELETE", "/v1/node/calls", proof, params=params)
        if resp.status != 200:
            unexpected_answer(resp, "DELETE /v1/node/calls")
        with answer_shape("DELETE /v1/node/calls"):
            return _int(resp.body["deleted"])

    async def set_expiry(self, user_id: str, agent_id: str, retention_s: float | None) -> int:
        """No-op: the Cloud moves the expiry of the agent's calls itself when the agent's `spec` changes."""
        return 0

    async def export(
        self, user_id: str, *, agent_id: str | None = None, since: float | None = None, until: float | None = None,
    ) -> AsyncIterator[tuple[CallRecord, list[EntryRecord]]]:
        """Every matching call with its entries, oldest first, in one streamed Cloud request. Not in the Protocol:
        check `calls.exports`. A stream cut before its end raises `CloudUnavailable` after what did arrive."""
        proof = require_proof(user_id)
        params = _params(agent=agent_id, since=since, until=until)
        lines = self._client.stream_lines("/v1/node/calls/export", user=proof, params=params)
        async with aclosing(lines):  # closing the export closes the HTTP stream now
            async for line in lines:
                with answer_shape("GET /v1/node/calls/export"):
                    call = call_record(proof.account, line["call"])
                    entries = entry_records(call.id, line["entries"])
                yield call, entries

    create = _unsupported("calls.create")  # the call path writes through CloudCallLog and the queue (grant)
    save = _unsupported("calls.save")
    add_entry = _unsupported("calls.add_entry")
    interrupt_unfinished = _unsupported("calls.interrupt_unfinished")
    entries_by_seal = _unsupported("calls.entries_by_seal")
    replace_entry = _unsupported("calls.replace_entry")
    cap_expiry = _unsupported("calls.cap_expiry")
    purge_expired = _unsupported("calls.purge_expired")
    compact = _unsupported("calls.compact")


class _Push:
    """One push target per device, kept by the Cloud for the device of the request's token (it never keeps the key
    either: the relay does)."""

    def __init__(self, client: CloudClient) -> None:
        self._client = client

    async def set(
        self, user_id: str, push_key: str, now: float, *, device_id: str | None = None, token_id: str | None = None,
    ) -> str | None:
        """The target of the request's device. KeyError if `device_id` names another device; ValueError when the Cloud
        refuses the key (`422`: not a registration of that Cloud; the route answers `422 invalid`). Returns None: the
        Cloud does not say which key it replaced."""
        proof = require_proof(user_id)
        if device_id is not None and device_id != proof.device:
            raise KeyError(device_id)
        resp = await _account_request(self._client, "PUT", "/v1/node/push-targets", proof, json={"push_key": push_key})
        if resp.status == 422:
            raise ValueError("the Cloud refused the push key")
        if resp.status not in (200, 204):
            unexpected_answer(resp, "PUT /v1/node/push-targets")
        return None

    async def clear(self, *, device_id: str | None = None, token_id: str | None = None) -> str | None:
        """Deletes the target of the request's device. Returns "cloud" (something was deleted, as far as the node
        knows: the Cloud answers 204 either way), so the route answers 204."""
        proof = current_proof.get()
        if proof is None or (device_id is not None and device_id != proof.device):
            raise ProofMismatch("no account proof for this device")
        resp = await _account_request(self._client, "DELETE", "/v1/node/push-targets", proof)
        if resp.status not in (200, 204):
            unexpected_answer(resp, "DELETE /v1/node/push-targets")
        return "cloud"

    forget = _unsupported("push.forget")
    for_device = _unsupported("push.for_device")
    for_apps = _unsupported("push.for_apps")


class _Meta:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def set(self, key: str, value: str) -> None:
        self._values[key] = value

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)
