"""Contract of the storage interface. Parametrized by adapter: the Cloud API adapter (E9) joins `params`."""

import sqlite3

import pytest

from wristcall.storage import AgentRecord, Conflict, Storage, open_sqlite_storage


@pytest.fixture(params=["sqlite"])
async def storage(request) -> Storage:
    st = open_sqlite_storage(":memory:")
    yield st
    await st.close()


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
    assert await storage.tokens.authenticate("th1", 2.0) is None
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
    # A device paired by 0.2.0 has no owner; the SQLite adapter exposes the raw table for this setup.
    st.db.execute("INSERT INTO devices (id, name, token_hash, created_at) VALUES ('d0', 'Old', 'h0', 1.0)")
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
    moved = await storage.agents.update(agent("u_a", "coach2", agent_id="ag_coach", position=0, updated_at=5.0))
    assert (moved.slug, moved.position, moved.created_at, moved.updated_at) == ("coach2", 0, 1.0, 5.0)
    assert [a.slug for a in await storage.agents.list("u_a")] == ["default", "coach2"]  # tie on position: creation order
    with pytest.raises(Conflict):
        await storage.agents.update(agent("u_a", "default", agent_id="ag_coach"))
    assert await storage.agents.count("u_a") == 2
    assert await storage.agents.delete("u_b", "ag_coach") is False
    assert await storage.agents.delete("u_a", "ag_coach") is True
    assert await storage.agents.count("u_a") == 1


async def test_call_type_is_checked_by_the_database(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        await storage.agents.create(agent("u_a", "x", call_type="podcast"))


async def test_meta(storage):
    assert await storage.meta.get("k") is None
    await storage.meta.set("k", "1")
    await storage.meta.set("k", "2")
    assert await storage.meta.get("k") == "2"
