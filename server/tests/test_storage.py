"""Contract of the storage interface. Parametrized by adapter: the Cloud API adapter (E9) joins `params`."""

import sqlite3
from dataclasses import replace

import pytest

from memory_storage import MemoryStorage
from wristcall.storage import AgentRecord, CallRecord, Conflict, LimitReached, Storage, open_sqlite_storage


@pytest.fixture(params=["sqlite", "memory"])
async def storage(request) -> Storage:
    st = open_sqlite_storage(":memory:") if request.param == "sqlite" else MemoryStorage()
    yield st
    await st.close()


def add_orphan(st, device_id: str, token_hash: str) -> None:
    """A device paired by 0.2.0 (no owner): only the adapters' own setup can create one."""
    if isinstance(st, MemoryStorage):
        st.devices.add_orphan(device_id, "Old", token_hash, 1.0)
    else:
        st.db.execute("INSERT INTO devices (id, name, token_hash, created_at) VALUES (?, 'Old', ?, 1.0)", (device_id, token_hash))


def agent(user_id: str, slug: str, agent_id: str | None = None, **over) -> AgentRecord:
    fields = dict(
        id=agent_id or f"ag_{slug}", user_id=user_id, slug=slug, display_name=slug.title(), icon="waveform",
        call_type="conversation", position=99, spec={"language": "en"}, created_at=1.0, updated_at=1.0,
    )
    return AgentRecord(**{**fields, **over})


async def test_users_crud(storage):
    a = await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.users.create("u_b", "bob", "Bob", 2.0)
    assert await storage.users.get("u_a") == a
    assert (await storage.users.by_handle("bob")).id == "u_b"
    assert [u.handle for u in await storage.users.list()] == ["alice", "bob"]
    with pytest.raises(Conflict):
        await storage.users.create("u_c", "alice", "Other", 3.0)
    renamed = await storage.users.update("u_a", handle="ally")
    assert renamed.handle == "ally" and renamed.display_name == "Alice"
    with pytest.raises(Conflict):
        await storage.users.update("u_a", handle="bob")
    assert await storage.users.update("u_missing", display_name="x") is None
    assert await storage.users.delete("u_b") is True
    assert await storage.users.delete("u_b") is False


async def test_deleting_a_user_cascades(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.devices.create("d1", "u_a", "Watch", "h1", 1.0)
    await storage.tokens.create("t1", "u_a", "cli", "th1", 1.0)
    await storage.agents.create(agent("u_a", "default"))
    assert await storage.pairing.add_code("12345678", "u_a", 9e9)
    await storage.users.delete("u_a")
    assert await storage.devices.by_token("h1") is None
    assert await storage.tokens.authenticate("th1", 200.0) is None
    assert await storage.agents.list("u_a") == []
    assert await storage.pairing.claim_code("12345678", 2.0, 5) is None


async def test_tokens(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    t = await storage.tokens.create("t1", "u_a", "laptop", "th1", 1.0)
    assert t.last_used_at is None
    used = await storage.tokens.authenticate("th1", 5.0)
    assert used.id == "t1" and used.last_used_at == 5.0
    assert await storage.tokens.authenticate("nope", 5.0) is None
    assert [x.id for x in await storage.tokens.list("u_a")] == ["t1"]
    assert await storage.tokens.revoke("t1", 6.0) is True
    assert await storage.tokens.revoke("t1", 7.0) is False
    assert await storage.tokens.authenticate("th1", 8.0) is None
    assert await storage.tokens.list("u_a") == []


async def test_devices(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.users.create("u_b", "bob", "Bob", 1.0)
    d = await storage.devices.create("d1", "u_a", "Watch", "h1", 1.0)
    await storage.devices.create("d2", "u_b", "Watch B", "h2", 2.0)
    assert await storage.devices.by_token("h1") == d
    assert [x.id for x in await storage.devices.list()] == ["d1", "d2"]
    assert [x.id for x in await storage.devices.list("u_b")] == ["d2"]
    assert await storage.devices.count("u_a") == 1
    assert await storage.devices.revoke("d1", 3.0, user_id="u_b") is False  # not hers to revoke
    assert await storage.devices.revoke("d1", 3.0, user_id="u_a") is True
    assert await storage.devices.by_token("h1") is None
    assert await storage.devices.revoke("d2", 3.0) is True
    assert await storage.devices.count("u_b") == 0


async def test_adopt_orphan_devices(storage):
    st = storage
    add_orphan(st, "d0", "h0")
    assert (await st.devices.by_token("h0")).user_id is None
    await st.users.create("u_a", "alice", "Alice", 1.0)
    assert await st.devices.adopt_orphans("u_a") == 1
    assert (await st.devices.by_token("h0")).user_id == "u_a"
    assert await st.devices.adopt_orphans("u_a") == 0


async def test_pairing_codes(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    assert await storage.pairing.add_code("11111111", "u_a", 100.0) is True
    assert await storage.pairing.add_code("11111111", "u_a", 100.0) is False
    assert await storage.pairing.claim_code("11111111", 50.0, 5) == "u_a"
    assert await storage.pairing.claim_code("11111111", 50.0, 5) is None  # single use
    await storage.pairing.add_code("22222222", "u_a", 100.0)
    for _ in range(5):
        await storage.pairing.count_failed_attempt(50.0)
    assert await storage.pairing.claim_code("22222222", 50.0, 5) is None  # blocked
    await storage.pairing.add_code("33333333", "u_a", 100.0)
    assert await storage.pairing.claim_code("33333333", 150.0, 5) is None  # expired
    await storage.pairing.purge(150.0)
    assert await storage.pairing.add_code("33333333", "u_a", 300.0) is True
    await storage.pairing.discard_code("33333333")
    assert await storage.pairing.claim_code("33333333", 200.0, 5) is None


async def test_pairing_requests(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.pairing.add_request("p1", "0001", "Watch", 1.0, 100.0)
    assert await storage.pairing.pending_ids(2.0) == {"0001"}
    (req,) = await storage.pairing.pending_by_id("0001", 2.0)
    assert (req.poll_hash, req.status, req.user_id) == ("p1", "pending", None)
    assert [r.request_id for r in await storage.pairing.list_pending(2.0)] == ["0001"]
    assert await storage.pairing.approve("p1", "u_a", 2.0, 200.0) is True
    assert await storage.pairing.approve("p1", "u_a", 2.0, 200.0) is False
    got = await storage.pairing.get_request("p1", 150.0)
    assert (got.status, got.user_id, got.expires_at) == ("approved", "u_a", 200.0)
    assert await storage.pairing.deliver("p1") is True
    assert await storage.pairing.deliver("p1") is False
    await storage.pairing.set_request_device("p1", "d1")
    assert await storage.pairing.get_request("p1", 250.0) is None  # expired
    assert await storage.pairing.list_pending(2.0) == []


async def test_agents(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.users.create("u_b", "bob", "Bob", 1.0)
    first = await storage.agents.create(agent("u_a", "default"))
    second = await storage.agents.create(agent("u_a", "coach", spec={"language": "pt", "vad": {"silence_ms": 900}}))
    await storage.agents.create(agent("u_b", "default", agent_id="ag_b_default"))
    assert (first.position, second.position) == (0, 1)  # appended, the given position is ignored
    assert second.spec == {"language": "pt", "vad": {"silence_ms": 900}}
    assert (await storage.agents.get("u_a", "coach")).id == "ag_coach"
    assert (await storage.agents.get("u_a", "ag_coach")).slug == "coach"
    assert await storage.agents.get("u_b", "coach") is None  # other user's agent
    assert [a.slug for a in await storage.agents.list("u_a")] == ["default", "coach"]
    with pytest.raises(Conflict):
        await storage.agents.create(agent("u_a", "coach", agent_id="ag_other"))
    renamed = await storage.agents.update(agent("u_a", "coach2", agent_id="ag_coach", position=0, updated_at=5.0))
    assert (renamed.slug, renamed.position, renamed.created_at, renamed.updated_at) == ("coach2", 1, 1.0, 5.0)
    assert [a.slug for a in await storage.agents.list("u_a")] == ["default", "coach2"]  # position is owned by move
    with pytest.raises(Conflict):
        await storage.agents.update(agent("u_a", "default", agent_id="ag_coach"))
    assert await storage.agents.count("u_a") == 2
    assert await storage.agents.delete("u_b", "ag_coach") is False
    assert await storage.agents.delete("u_a", "ag_coach") is True
    assert await storage.agents.count("u_a") == 1


async def test_call_type_is_checked_by_the_storage(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        await storage.agents.create(agent("u_a", "x", call_type="podcast"))


async def test_agent_limit_is_checked_on_create(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.agents.create(agent("u_a", "one"), max_count=2)
    await storage.agents.create(agent("u_a", "two"), max_count=2)
    with pytest.raises(LimitReached):
        await storage.agents.create(agent("u_a", "three"), max_count=2)
    assert await storage.agents.count("u_a") == 2
    await storage.agents.create(agent("u_a", "three"))  # no limit given


async def test_limit_is_checked_before_the_slug(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.agents.create(agent("u_a", "one"), max_count=2)
    await storage.agents.create(agent("u_a", "two"), max_count=2)
    with pytest.raises(LimitReached):
        await storage.agents.create(agent("u_a", "one", agent_id="ag_dup"), max_count=2)


async def test_update_never_writes_the_position(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.agents.create(agent("u_a", "a"))
    await storage.agents.create(agent("u_a", "b"))
    stale = await storage.agents.get("u_a", "a")  # position 0, read before a concurrent move
    await storage.agents.move("u_a", "ag_b", 0)
    await storage.agents.update(replace(stale, display_name="A", updated_at=5.0))
    assert [(a.slug, a.position) for a in await storage.agents.list("u_a")] == [("b", 0), ("a", 1)]


async def test_move_renumbers(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    for slug in ("a", "b", "c", "d"):
        await storage.agents.create(agent("u_a", slug))
    moved = await storage.agents.move("u_a", "ag_d", 1)
    assert moved.slug == "d" and moved.position == 1
    assert [(a.slug, a.position) for a in await storage.agents.list("u_a")] == [("a", 0), ("d", 1), ("b", 2), ("c", 3)]
    assert (await storage.agents.move("u_a", "ag_a", 99)).position == 3
    assert (await storage.agents.move("u_a", "ag_b", -5)).position == 0
    assert [a.slug for a in await storage.agents.list("u_a")] == ["b", "d", "c", "a"]
    assert await storage.agents.move("u_a", "ag_missing", 0) is None
    await storage.users.create("u_b", "bob", "Bob", 1.0)
    assert await storage.agents.move("u_b", "ag_a", 0) is None  # not hers


async def test_token_last_used_is_written_at_most_once_a_minute(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.tokens.create("t1", "u_a", "cli", "th1", 1.0)
    assert (await storage.tokens.authenticate("th1", 100.0)).last_used_at == 100.0
    assert (await storage.tokens.authenticate("th1", 130.0)).last_used_at == 100.0
    assert (await storage.tokens.authenticate("th1", 161.0)).last_used_at == 161.0
    assert (await storage.tokens.list("u_a"))[0].last_used_at == 161.0


async def test_assign_device(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.users.create("u_b", "bob", "Bob", 1.0)
    add_orphan(storage, "d0", "h0")
    assert await storage.devices.assign("d0", "u_b") is True
    assert (await storage.devices.by_token("h0")).user_id == "u_b"
    assert await storage.devices.assign("missing", "u_b") is False
    await storage.devices.revoke("d0", 2.0)
    assert await storage.devices.assign("d0", "u_a") is False


async def test_meta(storage):
    assert await storage.meta.get("k") is None
    await storage.meta.set("k", "1")
    await storage.meta.set("k", "2")
    assert await storage.meta.get("k") == "2"


def call(**over) -> CallRecord:
    fields = dict(
        id="c_1", user_id="u_a", agent_id="ag_1", device_id="d_1", call_type="one-shot", status="recording",
        created_at=1.0, updated_at=1.0,
    )
    return CallRecord(**{**fields, **over})


async def test_calls_create_get_and_save(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.users.create("u_b", "bob", "Bob", 1.0)
    created = await storage.calls.create(call())
    assert await storage.calls.get("u_a", "c_1") == created
    assert await storage.calls.get("u_b", "c_1") is None
    done = replace(
        created, status="delivered", text="buy milk", attempts=2, last_http_status=204,
        ended_at=2.0, finished_at=3.0, updated_at=3.0,
        # Not changeable by save:
        user_id="u_b", agent_id="ag_x", device_id=None, call_type="monologue", created_at=9.0,
    )
    saved = await storage.calls.save(done)
    assert saved == replace(done, user_id="u_a", agent_id="ag_1", device_id="d_1", call_type="one-shot", created_at=1.0)
    assert await storage.calls.get("u_a", "c_1") == saved
    with pytest.raises(KeyError):
        await storage.calls.save(call(id="c_gone"))


async def test_calls_interrupted_at_startup_keep_their_text(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.calls.create(call(id="c_rec"))
    await storage.calls.create(call(id="c_proc", status="processing", text="half"))
    await storage.calls.create(call(id="c_ok", status="delivered", finished_at=2.0))
    assert await storage.calls.interrupt_unfinished(5.0) == 2
    proc = await storage.calls.get("u_a", "c_proc")
    assert (proc.status, proc.error, proc.text, proc.finished_at) == ("failed", "interrupted", "half", 5.0)
    assert (await storage.calls.get("u_a", "c_rec")).status == "failed"
    assert (await storage.calls.get("u_a", "c_ok")).status == "delivered"


async def test_deleting_a_user_deletes_their_calls(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.calls.create(call())
    await storage.users.delete("u_a")
    assert await storage.calls.get("u_a", "c_1") is None
