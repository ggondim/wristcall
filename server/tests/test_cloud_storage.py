"""CloudStorage: the server's Storage over the Cloud's node API (cloud mode), against the in-process FakeCloud."""

import json
import logging
from dataclasses import replace

import httpx
import pytest
from fake_cloud import CLOUD_URL, FULL_PLAN, FakeCloud

from wristcall.cloud_node.client import CloudClient
from wristcall.cloud_node.proof import ProofMismatch, UserProof, current_proof
from wristcall.cloud_node.seal import SealCodec
from wristcall.storage import (
    AgentRecord,
    CallOpen,
    CallRecord,
    CloudUnavailable,
    Conflict,
    DeviceRevoked,
    EntryRecord,
    LimitReached,
    NotSupported,
    PlanRequired,
)
from wristcall.storage.cloud import CloudStorage

NODE_URL = "https://node.test"
SECRET = "wc_node_" + "s" * 43
KEY = bytes(range(32))
ACCOUNT, DEVICE = "acc-a", "dev-a"
HASH1, HASH2 = "x0123456789abcdef", "xfedcba9876543210"
PUSH_KEY = "wc_push_" + "k" * 43


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def fake() -> FakeCloud:
    fake = FakeCloud(NODE_URL, SECRET)
    fake.plans[ACCOUNT] = FULL_PLAN
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def st(fake: FakeCloud, clock: Clock):
    storage = CloudStorage(CloudClient(CLOUD_URL, SECRET, fake.http()), SealCodec(KEY), now=clock)
    yield storage
    await storage.close()


def proof(fake: FakeCloud, account: str = ACCOUNT, device: str = DEVICE, platform: str = "ios") -> UserProof:
    return UserProof(account, device, platform, fake.user_token(account, device, platform))


@pytest.fixture(autouse=True)
def as_account(fake: FakeCloud):
    token = current_proof.set(proof(fake))
    yield
    current_proof.reset(token)


def agent(slug: str, agent_id: str, **over) -> AgentRecord:
    fields = dict(
        id=agent_id, user_id=ACCOUNT, slug=slug, display_name=slug.title(), icon="waveform", call_type="conversation",
        position=0, spec={"language": "en", "stt": {"provider": "stt"}}, created_at=1.0, updated_at=1.0,
    )
    return AgentRecord(**{**fields, **over})


AG1, AG2 = "ag_000000000001", "ag_000000000002"


def custom_spec() -> dict:
    return {
        "language": "pt",
        "stt": {"type": "openai_stt", "base_url": "https://stt.example.com/v1", "api_key": "sk-secret-stt"},
        "action": {"provider": "llm"},
        "tts": {"type": "openai_tts", "base_url": "https://tts.example.com/v1", "api_key": "sk-secret-tts"},
        "retention_days": 7,
    }


def gets(fake: FakeCloud, path: str) -> list[dict]:
    return [r for r in fake.requests if r["method"] == "GET" and r["path"] == path]


# --- agents ----------------------------------------------------------------------------------------------------


async def test_spec_is_sealed_at_rest(st: CloudStorage, fake: FakeCloud):
    created = await st.agents.create(agent("coach", AG1, spec=custom_spec()))
    assert created.spec == custom_spec() and created.user_id == ACCOUNT
    stored = json.dumps(fake.agents[AG1]["spec"])
    for secret in ("sk-secret-stt", "sk-secret-tts", "stt.example.com", "tts.example.com"):
        assert secret not in stored
    assert fake.agents[AG1]["spec"]["action"] == {"provider": "llm"} and fake.agents[AG1]["spec"]["retention_days"] == 7
    assert (await st.agents.get(ACCOUNT, "coach")).spec == custom_spec()
    assert (await st.agents.list(ACCOUNT))[0].spec == custom_spec()


async def test_agents_cache_and_etag(st: CloudStorage, fake: FakeCloud, clock: Clock):
    await st.agents.create(agent("coach", AG1))
    assert [a.slug for a in await st.agents.list(ACCOUNT)] == ["coach"]
    assert [a.slug for a in await st.agents.list(ACCOUNT)] == ["coach"]
    assert await st.agents.count(ACCOUNT) == 1
    assert len(gets(fake, "/v1/node/agents")) == 1  # one trip within 30 s
    await st.agents.update(agent("coach", AG1, display_name="Coach 2"))
    assert [a.display_name for a in await st.agents.list(ACCOUNT)] == ["Coach 2"]  # a write drops the cache
    listings = gets(fake, "/v1/node/agents")
    assert len(listings) == 2 and "if-none-match" not in listings[-1]["headers"]
    clock.t += 31
    assert [a.display_name for a in await st.agents.list(ACCOUNT)] == ["Coach 2"]
    listings = gets(fake, "/v1/node/agents")
    assert len(listings) == 3 and listings[-1]["headers"]["if-none-match"].startswith('"')  # answered 304
    assert len(gets(fake, "/v1/node/agents")) == 3
    await st.agents.list(ACCOUNT)
    assert len(gets(fake, "/v1/node/agents")) == 3  # the 304 refreshed the cache


async def test_cache_is_per_account(st: CloudStorage, fake: FakeCloud):
    await st.agents.create(agent("coach", AG1))
    await st.agents.list(ACCOUNT)
    fake.plans["acc-b"] = FULL_PLAN
    current_proof.set(proof(fake, "acc-b", "dev-b"))
    assert await st.agents.list("acc-b") == []


async def test_agent_writes_map_cloud_errors(st: CloudStorage, fake: FakeCloud):
    await st.agents.create(agent("one", AG1))
    with pytest.raises(Conflict):
        await st.agents.create(agent("one", AG2))
    with pytest.raises(ValueError):
        await st.agents.create(agent("two", AG2, call_type="podcast"))  # 422 invalid
    fake.max_agents = 1
    with pytest.raises(LimitReached):
        await st.agents.create(agent("two", AG2))  # the Cloud's own cap (403 limit)
    with pytest.raises(LimitReached):
        await st.agents.create(agent("two", AG2), max_count=1)
    with pytest.raises(KeyError):
        await st.agents.update(agent("gone", AG2))
    assert await st.agents.delete(ACCOUNT, AG2) is False
    assert await st.agents.move(ACCOUNT, AG2, 0) is None
    assert await st.agents.get(ACCOUNT, "nope") is None
    assert await st.agents.get(ACCOUNT, "../calls") is None  # not an id or slug: never sent as a path


async def test_patch_sends_only_agent_fields(st: CloudStorage, fake: FakeCloud):
    await st.agents.create(agent("coach", AG1, color="blue"))
    updated = await st.agents.update(agent("coach2", AG1, position=7, color=None, spec=custom_spec()))
    assert (updated.slug, updated.color, updated.position, updated.spec) == ("coach2", None, 0, custom_spec())
    patch = next(r for r in fake.requests if r["method"] == "PATCH")
    assert patch["path"] == f"/v1/node/agents/{AG1}"
    # the body is checked by the fake (it refuses any other field); position, ids and times never travel
    assert fake.agents[AG1]["color"] is None and fake.agents[AG1]["slug"] == "coach2"


async def test_move_returns_the_moved_agent(st: CloudStorage, fake: FakeCloud):
    for n, slug in enumerate(("a", "b", "c")):
        await st.agents.create(agent(slug, f"ag_00000000000{n}"))
    moved = await st.agents.move(ACCOUNT, "ag_000000000002", 0)
    assert (moved.slug, moved.position) == ("c", 0)
    assert [a.slug for a in await st.agents.list(ACCOUNT)] == ["c", "a", "b"]
    put = next(r for r in fake.requests if r["method"] == "PUT")
    assert put["path"] == "/v1/node/agents/order"
    assert (await st.agents.move(ACCOUNT, "ag_000000000000", 10**30)).position == 2  # clamped before sending


async def test_unopenable_agent_is_left_out(st: CloudStorage, fake: FakeCloud, caplog):
    await st.agents.create(agent("good", AG1))
    # whoever writes the Cloud's database cannot point the audio elsewhere: a clear-text endpoint is refused
    fake.agents[AG2] = {**fake.agents[AG1], "id": AG2, "slug": "bad", "position": 1,
                        "spec": {"language": "en", "stt": {"type": "openai_stt", "base_url": "https://evil.example"}}}
    # an envelope sealed for another account does not open either
    other = SealCodec(KEY).seal_spec(custom_spec(), "acc-b", "ag_000000000003")
    fake.agents["ag_000000000003"] = {**fake.agents[AG1], "id": "ag_000000000003", "slug": "moved", "position": 2,
                                      "spec": other}
    # nor a spec the server would refuse
    fake.agents["ag_000000000004"] = {**fake.agents[AG1], "id": "ag_000000000004", "slug": "odd", "position": 3,
                                      "spec": {"language": "en"}}
    with caplog.at_level(logging.WARNING, logger="wristcall.storage.cloud"):
        assert [a.slug for a in await st.agents.list(ACCOUNT)] == ["good"]
        assert await st.agents.get(ACCOUNT, "bad") is None
        assert await st.agents.get(ACCOUNT, "moved") is None
        assert await st.agents.get(ACCOUNT, "odd") is None
    assert AG2 in caplog.text and "evil" not in caplog.text


async def test_wrong_account_never_reaches_cloud(st: CloudStorage, fake: FakeCloud):
    with pytest.raises(PermissionError):
        await st.agents.list("acc-b")
    with pytest.raises(PermissionError):
        await st.agents.get("acc-b", "coach")
    with pytest.raises(PermissionError):
        await st.agents.create(agent("coach", AG1, user_id="acc-b"))
    with pytest.raises(PermissionError):
        await st.calls.list("acc-b")
    with pytest.raises(PermissionError):
        await st.calls.get("acc-b", "c_1")
    with pytest.raises(PermissionError):
        await st.push.set("acc-b", PUSH_KEY, 1.0, device_id=DEVICE)
    with pytest.raises(PermissionError):
        await st.users.get("acc-b")
    with pytest.raises(PermissionError):
        async for _ in st.calls.export("acc-b"):
            pass
    current_proof.set(None)
    with pytest.raises(ProofMismatch):
        await st.agents.list(ACCOUNT)
    with pytest.raises(ProofMismatch):
        await st.push.clear(device_id=DEVICE)
    assert fake.requests == []


async def test_records_take_the_account_from_the_proof(st: CloudStorage, fake: FakeCloud):
    await st.agents.create(agent("coach", AG1))
    fake.agents[AG1]["account"] = ACCOUNT  # the stored owner; the answer never carries it
    cid = fake.seed_call(ACCOUNT, agent_id=AG1)
    assert (await st.agents.get(ACCOUNT, AG1)).user_id == ACCOUNT
    assert (await st.calls.get(ACCOUNT, cid)).user_id == ACCOUNT


async def test_virtual_user(st: CloudStorage, fake: FakeCloud):
    user = await st.users.get(ACCOUNT)
    assert (user.id, user.handle, user.display_name, user.created_at) == (ACCOUNT, ACCOUNT, "", 0.0)
    assert fake.requests == []


# --- calls -----------------------------------------------------------------------------------------------------


async def test_calls_get_and_entries_share_one_trip(st: CloudStorage, fake: FakeCloud):
    cid = fake.seed_call(ACCOUNT, agent_id=AG1, created_at=50.0, entries=[
        {"seq": 1, "role": "agent", "text": "v1.b", "sealed": True, "at": 52.0},
        {"seq": 0, "role": "user", "text": "v1.a", "sealed": True, "at": 51.0, "term_hashes": [HASH1]},
    ])
    record = await st.calls.get(ACCOUNT, cid)
    assert isinstance(record, CallRecord)
    assert (record.id, record.user_id, record.agent_id, record.status, record.created_at, record.delivery) == (
        cid, ACCOUNT, AG1, "delivered", 50.0, "history")
    entries = await st.calls.entries(ACCOUNT, cid)
    assert entries == [
        EntryRecord(call_id=cid, seq=0, role="user", text="v1.a", sealed=True, error=None, at=51.0),
        EntryRecord(call_id=cid, seq=1, role="agent", text="v1.b", sealed=True, error=None, at=52.0),
    ]
    assert len(gets(fake, f"/v1/node/calls/{cid}")) == 1  # get + entries = one trip
    await st.calls.entries(ACCOUNT, cid)
    assert len(gets(fake, f"/v1/node/calls/{cid}")) == 2  # the memo serves one entries() after a get()
    await st.calls.get(ACCOUNT, cid)
    current_proof.set(proof(fake))  # another request
    await st.calls.entries(ACCOUNT, cid)
    assert len(gets(fake, f"/v1/node/calls/{cid}")) == 4
    assert await st.calls.get(ACCOUNT, "c_missing") is None
    assert await st.calls.entries(ACCOUNT, "c_missing") == []
    assert await st.calls.get(ACCOUNT, "../agents") is None


async def test_call_memo_is_short_lived(st: CloudStorage, fake: FakeCloud, clock: Clock):
    cid = fake.seed_call(ACCOUNT)
    await st.calls.get(ACCOUNT, cid)
    clock.t += 10
    await st.calls.entries(ACCOUNT, cid)
    assert len(gets(fake, f"/v1/node/calls/{cid}")) == 2


async def test_calls_list_maps_query(st: CloudStorage, fake: FakeCloud):
    a = fake.seed_call(ACCOUNT, agent_id=AG1, created_at=10.0, entries=[{"seq": 0, "term_hashes": [HASH1]}])
    fake.seed_call(ACCOUNT, agent_id=AG1, created_at=20.0, entries=[{"seq": 0, "term_hashes": [HASH2]}])
    fake.seed_call(ACCOUNT, agent_id=AG2, created_at=15.0, entries=[{"seq": 0, "term_hashes": [HASH1]}])
    fake.seed_call("acc-b", agent_id=AG1, created_at=12.0, entries=[{"seq": 0, "term_hashes": [HASH1]}])
    found = await st.calls.list(
        ACCOUNT, agent_id=AG1, since=5.0, until=30.5, terms=[["word", HASH1]], before=(25.0, "c_z"), limit=10,
    )
    assert [c.id for c in found] == [a] and found[0].user_id == ACCOUNT
    params = dict(fake.requests[-1]["params"])
    assert params == {"agent": AG1, "since": "5.0", "until": "30.5", "before_at": "25.0", "before_id": "c_z",
                      "limit": "10", "terms": HASH1}
    assert [c.created_at for c in await st.calls.list(ACCOUNT)] == [20.0, 15.0, 10.0]
    assert fake.requests[-1]["params"] == [("limit", "50")]
    two = await st.calls.list(ACCOUNT, terms=[[HASH1, "w", HASH2], ["x", HASH1]])
    assert [c.created_at for c in two] == [15.0, 10.0]
    assert fake.requests[-1]["params"] == [("terms", f"{HASH1},{HASH2}"), ("terms", HASH1), ("limit", "50")]
    sent = len(fake.requests)
    assert await st.calls.list(ACCOUNT, terms=[]) == []
    assert await st.calls.list(ACCOUNT, terms=[["word"]]) == []  # a group with no hash matches nothing
    assert await st.calls.list(ACCOUNT, terms=[[HASH1], []]) == []
    assert len(fake.requests) == sent  # resolved without the Cloud


async def test_delete_open_call_is_call_open(st: CloudStorage, fake: FakeCloud):
    open_call = fake.seed_call(ACCOUNT, open=True)
    done = fake.seed_call(ACCOUNT)
    with pytest.raises(CallOpen):
        await st.calls.delete(ACCOUNT, open_call)
    assert await st.calls.get(ACCOUNT, done) is not None
    assert await st.calls.delete(ACCOUNT, done) is True
    assert await st.calls.get(ACCOUNT, done) is None  # the memo of a deleted call is dropped
    assert await st.calls.delete(ACCOUNT, done) is False
    assert await st.calls.delete(ACCOUNT, "c/../x") is False


async def test_delete_all_by_agent_and_all(st: CloudStorage, fake: FakeCloud):
    fake.seed_call(ACCOUNT, agent_id=AG1)
    fake.seed_call(ACCOUNT, agent_id=AG1)
    fake.seed_call(ACCOUNT, agent_id=AG2)
    fake.seed_call(ACCOUNT, agent_id=AG2, open=True)
    other = fake.seed_call("acc-b", agent_id=AG1)
    assert await st.calls.delete_all(ACCOUNT, AG1) == 2
    assert fake.requests[-1]["params"] == [("agent", AG1)]
    assert await st.calls.delete_all(ACCOUNT) == 1  # the open call stays
    assert fake.requests[-1]["params"] == [("all", "true")]
    assert other in fake.calls and len(fake.calls) == 2


async def test_set_expiry_is_a_no_op(st: CloudStorage, fake: FakeCloud):
    assert await st.calls.set_expiry(ACCOUNT, AG1, 86400.0) == 0
    assert fake.requests == []


async def test_export_yields_records(st: CloudStorage, fake: FakeCloud):
    assert st.calls.exports is True
    old = fake.seed_call(ACCOUNT, agent_id=AG1, created_at=10.0, entries=[{"seq": 0, "text": "v1.x", "sealed": True}])
    new = fake.seed_call(ACCOUNT, agent_id=AG1, created_at=20.0)
    fake.seed_call(ACCOUNT, agent_id=AG2, created_at=15.0)
    fake.seed_call("acc-b", agent_id=AG1, created_at=12.0)
    got = [(c, e) async for c, e in st.calls.export(ACCOUNT, agent_id=AG1, since=1.0, until=99.0)]
    assert [c.id for c, _ in got] == [old, new]  # oldest first
    assert all(isinstance(c, CallRecord) and c.user_id == ACCOUNT for c, _ in got)
    assert got[0][1] == [EntryRecord(call_id=old, seq=0, role="user", text="v1.x", sealed=True, error=None, at=10.0)]
    assert got[1][1] == []
    request = fake.requests[-1]
    assert request["path"] == "/v1/node/calls/export"
    assert request["params"] == [("agent", AG1), ("since", "1.0"), ("until", "99.0")]


@pytest.mark.parametrize("name, args", [
    ("calls.create", (None,)),
    ("calls.save", (None,)),
    ("calls.add_entry", (ACCOUNT, None, [])),
    ("calls.interrupt_unfinished", (1.0,)),
    ("calls.entries_by_seal", (True, 10)),
    ("calls.replace_entry", (None, [])),
    ("calls.cap_expiry", (1.0,)),
    ("calls.purge_expired", (1.0,)),
    ("calls.compact", ()),
    ("users.create", (ACCOUNT, "h", "n", 1.0)),
    ("users.by_handle", ("h",)),
    ("users.list", ()),
    ("users.update", (ACCOUNT,)),
    ("users.delete", (ACCOUNT,)),
    ("users.link_central", (ACCOUNT, "s", 1.0)),
    ("users.unlink_central", (ACCOUNT, 1.0)),
    ("users.by_central", ("s",)),
    ("tokens.create", ("t", ACCOUNT, "n", "h", 1.0)),
    ("tokens.authenticate", ("h", 1.0)),
    ("tokens.list", (ACCOUNT,)),
    ("tokens.revoke", ("t", 1.0)),
    ("devices.create", ("d", ACCOUNT, "n", "h", 1.0)),
    ("devices.by_token", ("h",)),
    ("devices.list", (ACCOUNT,)),
    ("devices.count", (ACCOUNT,)),
    ("devices.revoke", ("d", 1.0)),
    ("devices.adopt_orphans", (ACCOUNT,)),
    ("devices.assign", ("d", ACCOUNT)),
    ("pairing.purge", (1.0,)),
    ("pairing.add_code", ("c", ACCOUNT, 1.0)),
    ("pairing.claim_code", ("c", 1.0, 3)),
    ("pairing.add_request", ("p", "r", "n", 1.0, 2.0)),
    ("pairing.list_pending", (1.0,)),
    ("push.forget", (PUSH_KEY,)),
    ("push.for_device", (DEVICE,)),
    ("push.for_apps", (ACCOUNT,)),
])
async def test_not_supported_methods(st: CloudStorage, fake: FakeCloud, name: str, args: tuple):
    store, method = name.split(".")
    with pytest.raises(NotSupported):
        await getattr(getattr(st, store), method)(*args)
    assert fake.requests == []


# --- push targets and meta -------------------------------------------------------------------------------------


async def test_push_targets_round_trip(st: CloudStorage, fake: FakeCloud):
    assert await st.push.set(ACCOUNT, PUSH_KEY, 1.0, device_id=DEVICE) is None
    assert fake.push_targets == {f"{ACCOUNT}:{DEVICE}": PUSH_KEY}
    assert await st.push.set(ACCOUNT, PUSH_KEY, 1.0) is None  # the proof names the device (an app's principal)
    with pytest.raises(ValueError):
        await st.push.set(ACCOUNT, "wc_push_short", 1.0, device_id=DEVICE)  # Cloud 422: the route answers 422
    with pytest.raises(KeyError):
        await st.push.set(ACCOUNT, PUSH_KEY, 1.0, device_id="dev-other")  # not the device of this request
    assert await st.push.clear(device_id=DEVICE) == "cloud"
    assert fake.push_targets == {}
    assert await st.push.clear() == "cloud"


async def test_meta_in_memory(st: CloudStorage, fake: FakeCloud):
    assert await st.meta.get("k") is None
    await st.meta.set("k", "v")
    assert await st.meta.get("k") == "v"
    await st.meta.delete("k")
    assert await st.meta.get("k") is None
    assert fake.requests == []


# --- errors ----------------------------------------------------------------------------------------------------


async def test_plan_and_revocation_errors(st: CloudStorage, fake: FakeCloud):
    del fake.plans[ACCOUNT]
    with pytest.raises(PlanRequired):
        await st.agents.create(agent("coach", AG1))
    assert await st.agents.list(ACCOUNT) == []  # reading needs no plan
    fake.revoked.add(DEVICE)
    with pytest.raises(DeviceRevoked):
        await st.calls.list(ACCOUNT)
    with pytest.raises(DeviceRevoked):
        await st.agents.get(ACCOUNT, "coach")
    fake.revoked.clear()
    fake.down = True
    with pytest.raises(CloudUnavailable):
        await st.agents.get(ACCOUNT, "coach")
    with pytest.raises(CloudUnavailable):
        await st.calls.delete_all(ACCOUNT)
    with pytest.raises(CloudUnavailable):
        await st.push.clear()
    fake.down = False
    fake.retry_after = 5
    with pytest.raises(CloudUnavailable):
        await st.calls.get(ACCOUNT, "c_1")


async def test_fresh_listing_is_served_while_the_cloud_is_down(st: CloudStorage, fake: FakeCloud, clock: Clock):
    await st.agents.create(agent("coach", AG1))
    await st.agents.list(ACCOUNT)
    fake.down = True
    assert [a.slug for a in await st.agents.list(ACCOUNT)] == ["coach"]  # still fresh
    clock.t += 31
    with pytest.raises(CloudUnavailable):
        await st.agents.list(ACCOUNT)


class RacingClient(CloudClient):
    """Runs `during` once, after the Cloud answered the first agent listing and before the adapter reads it."""

    during = None

    async def request(self, method, path, **kwargs):
        resp = await super().request(method, path, **kwargs)
        if method == "GET" and path == "/v1/node/agents" and self.during is not None:
            during, self.during = self.during, None
            await during()
        return resp


async def test_listing_in_flight_during_a_write_is_not_cached(fake: FakeCloud):
    client = RacingClient(CLOUD_URL, SECRET, fake.http())
    st = CloudStorage(client, SealCodec(KEY))
    await st.agents.create(agent("coach", AG1))

    async def rename():
        await st.agents.update(agent("coach", AG1, display_name="Renamed"))

    client.during = rename
    assert [a.display_name for a in await st.agents.list(ACCOUNT)] == ["Coach"]  # read before the write
    assert [a.display_name for a in await st.agents.list(ACCOUNT)] == ["Renamed"]  # not served from the cache


async def test_many_accounts_writing_never_let_a_stale_listing_in(fake: FakeCloud):
    # The write counter once reset itself past 4096 accounts: the account's count could come back to the value a
    # listing in flight had seen, and that stale listing went into the cache.
    client = RacingClient(CLOUD_URL, SECRET, fake.http())
    st = CloudStorage(client, SealCodec(KEY))
    await st.agents.create(agent("coach", AG1))

    async def others_then_rename():
        for i in range(4096):
            st.agents._changed(f"acc-other-{i}")  # writes of other accounts, without 4096 trips to the fake
        await st.agents.update(agent("coach", AG1, display_name="Renamed"))

    client.during = others_then_rename
    assert [a.display_name for a in await st.agents.list(ACCOUNT)] == ["Coach"]  # read before the write
    assert [a.display_name for a in await st.agents.list(ACCOUNT)] == ["Renamed"]  # not served from the cache


def odd_cloud(answers: dict[tuple[str, str], httpx.Response]) -> CloudStorage:
    def handler(request: httpx.Request) -> httpx.Response:
        return answers[(request.method, request.url.path)]

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CloudStorage(CloudClient(CLOUD_URL, SECRET, http), SealCodec(KEY))


@pytest.mark.parametrize("method, path, body, call", [
    ("GET", "/v1/node/agents", {"etag": '"x"'}, lambda s: s.agents.list(ACCOUNT)),
    ("GET", "/v1/node/agents", {"agents": [{"id": AG1}], "etag": '"x"'}, lambda s: s.agents.list(ACCOUNT)),
    ("GET", "/v1/node/agents/coach", {"id": AG1, "slug": 3}, lambda s: s.agents.get(ACCOUNT, "coach")),
    ("GET", "/v1/node/calls", {"calls": "no"}, lambda s: s.calls.list(ACCOUNT)),
    ("GET", "/v1/node/calls", {"calls": [{"id": "c_1"}]}, lambda s: s.calls.list(ACCOUNT)),
    ("GET", "/v1/node/calls/c_1", {"call": {}}, lambda s: s.calls.get(ACCOUNT, "c_1")),
    ("DELETE", "/v1/node/calls", {"deleted": "2"}, lambda s: s.calls.delete_all(ACCOUNT)),
    ("DELETE", "/v1/node/calls", {}, lambda s: s.calls.delete_all(ACCOUNT)),
    ("GET", "/v1/node/agents", ["no"], lambda s: s.agents.list(ACCOUNT)),
])
async def test_answer_without_expected_fields_is_cloud_unavailable(method, path, body, call):
    st = odd_cloud({(method, path): httpx.Response(200, json=body)})
    with pytest.raises(CloudUnavailable):
        await call(st)


@pytest.mark.parametrize("status, call", [
    (404, lambda s: s.agents.list(ACCOUNT)),
    (418, lambda s: s.calls.list(ACCOUNT)),
    (403, lambda s: s.calls.delete_all(ACCOUNT)),
])
async def test_unexpected_status_is_cloud_unavailable(status, call):
    answers = {(m, p): httpx.Response(status, json={"error": "teapot", "message": "x"})
               for m, p in (("GET", "/v1/node/agents"), ("GET", "/v1/node/calls"), ("DELETE", "/v1/node/calls"))}
    with pytest.raises(CloudUnavailable):
        await call(odd_cloud(answers))


async def test_record_survives_replace(st: CloudStorage):
    created = await st.agents.create(agent("coach", AG1))
    again = await st.agents.update(replace(created, display_name="Other"))
    assert again.display_name == "Other" and again.id == AG1
