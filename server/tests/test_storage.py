"""Contract of the storage interface. Parametrized by adapter: the Cloud API adapter (E9) joins `params`."""

import sqlite3
from dataclasses import replace

import pytest

from memory_storage import MemoryStorage
from wristcall.storage import AgentRecord, CallRecord, Conflict, EntryRecord, LimitReached, Storage, open_sqlite_storage


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
    created = await storage.calls.create(call(agent_slug="note", agent_name="Note", expires_at=99.0))
    assert await storage.calls.get("u_a", "c_1") == created
    assert await storage.calls.get("u_b", "c_1") is None
    done = replace(
        created, status="delivered", attempts=2, last_http_status=204,
        ended_at=2.0, finished_at=3.0, updated_at=3.0,
        # Not changeable by save:
        user_id="u_b", agent_id="ag_x", device_id=None, call_type="monologue", created_at=9.0,
        agent_slug="x", agent_name="X", expires_at=None,
    )
    saved = await storage.calls.save(done)
    assert saved == replace(
        done, user_id="u_a", agent_id="ag_1", device_id="d_1", call_type="one-shot", created_at=1.0,
        agent_slug="note", agent_name="Note", expires_at=99.0,
    )
    assert await storage.calls.get("u_a", "c_1") == saved
    with pytest.raises(KeyError):
        await storage.calls.save(call(id="c_gone"))


async def test_calls_interrupted_at_startup(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.calls.create(call(id="c_rec"))
    await storage.calls.create(call(id="c_proc", status="processing", ended_at=2.0))
    await storage.calls.create(call(id="c_ok", status="delivered", finished_at=2.0))
    await storage.calls.create(call(id="c_conv", call_type="conversation"))
    await storage.calls.create(call(id="c_conv_done", call_type="conversation", status="ended"))
    assert await storage.calls.interrupt_unfinished(5.0) == 3
    proc = await storage.calls.get("u_a", "c_proc")
    assert (proc.status, proc.error, proc.ended_at, proc.finished_at) == ("failed", "interrupted", 2.0, 5.0)
    rec = await storage.calls.get("u_a", "c_rec")
    assert (rec.status, rec.ended_at) == ("failed", 5.0)
    conv = await storage.calls.get("u_a", "c_conv")
    assert (conv.status, conv.error, conv.ended_at) == ("ended", "interrupted", 5.0)
    assert (await storage.calls.get("u_a", "c_ok")).status == "delivered"
    assert (await storage.calls.get("u_a", "c_conv_done")).error is None


def entry(call_id="c_1", seq=0, role="user", text="hi", **over) -> EntryRecord:
    return EntryRecord(**{**dict(call_id=call_id, seq=seq, role=role, text=text, sealed=False, error=None, at=2.0), **over})


async def test_entries_are_kept_in_order_and_only_for_the_owner(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.users.create("u_b", "bob", "Bob", 1.0)
    await storage.calls.create(call(call_type="conversation"))
    assert await storage.calls.add_entry("u_a", entry(seq=1, role="agent", text="hello", error="tts_failed"), ["hello"])
    assert await storage.calls.add_entry("u_a", entry(seq=0, text="wc1secret", sealed=True), ["x0123456789abcdef"])
    assert await storage.calls.add_entry("u_a", entry(seq=2, text=None, error="stt_failed"), [])
    got = await storage.calls.entries("u_a", "c_1")
    assert [(e.seq, e.role, e.text, e.sealed, e.error) for e in got] == [
        (0, "user", "wc1secret", True, None), (1, "agent", "hello", False, "tts_failed"), (2, "user", None, False, "stt_failed"),
    ]
    assert await storage.calls.entries("u_b", "c_1") == []
    assert not await storage.calls.add_entry("u_b", entry(seq=3), ["hi"])  # not bob's call
    assert not await storage.calls.add_entry("u_a", entry(call_id="c_gone"), ["hi"])


async def history(storage) -> None:
    """alice: c_1 (ag_1, t=10, "buy milk"), c_2 (ag_2, t=20, "milk and bread" / agent "noted"), c_3 (ag_1, t=30, no text).
    bob: c_9 (t=15, "milk")."""
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    await storage.users.create("u_b", "bob", "Bob", 1.0)
    for cid, agent_id, at, texts in (
        ("c_1", "ag_1", 10.0, [("user", "buy milk")]),
        ("c_2", "ag_2", 20.0, [("user", "milk and bread"), ("agent", "noted")]),
        ("c_3", "ag_1", 30.0, []),
    ):
        await storage.calls.create(call(id=cid, agent_id=agent_id, status="ended", created_at=at, updated_at=at))
        for seq, (role, text) in enumerate(texts):
            await storage.calls.add_entry("u_a", entry(cid, seq, role, text), text.split())
    await storage.calls.create(call(id="c_9", user_id="u_b", status="ended", created_at=15.0))
    await storage.calls.add_entry("u_b", entry("c_9"), ["milk"])


async def ids(storage, **kw) -> list[str]:
    return [c.id for c in await storage.calls.list("u_a", **kw)]


async def test_list_is_newest_first_and_filtered(storage):
    await history(storage)
    assert await ids(storage) == ["c_3", "c_2", "c_1"]
    assert await ids(storage, agent_id="ag_1") == ["c_3", "c_1"]
    assert await ids(storage, since=20.0) == ["c_3", "c_2"]
    assert await ids(storage, until=20.0) == ["c_1"]
    assert await ids(storage, since=10.0, until=30.0, agent_id="ag_2") == ["c_2"]
    assert await ids(storage, limit=2) == ["c_3", "c_2"]
    assert await ids(storage, before="c_2") == ["c_1"]
    assert await ids(storage, before="c_9") == []  # bob's call is no anchor for alice
    assert await ids(storage, before="c_missing") == []


async def test_list_searches_terms_in_any_entry(storage):
    await history(storage)
    assert await ids(storage, terms=[["milk"]]) == ["c_2", "c_1"]
    assert await ids(storage, terms=[["milk"], ["bread"]]) == ["c_2"]
    assert await ids(storage, terms=[["noted"]]) == ["c_2"]  # the agent's answer too
    assert await ids(storage, terms=[["milk"], ["noted"]]) == []  # every group in one entry
    assert await ids(storage, terms=[["nothing", "bread"]]) == ["c_2"]  # alternatives
    assert await ids(storage, terms=[["milk"]], agent_id="ag_1") == ["c_1"]
    assert await ids(storage, terms=[]) == []


async def test_same_time_calls_page_by_id(storage):
    await storage.users.create("u_a", "alice", "Alice", 1.0)
    for cid in ("c_a", "c_b", "c_c"):
        await storage.calls.create(call(id=cid, created_at=5.0))
    assert await ids(storage, limit=2) == ["c_c", "c_b"]
    assert await ids(storage, before="c_b") == ["c_a"]


async def test_deleting_calls_takes_entries_and_index_along(storage):
    await history(storage)
    assert await storage.calls.delete("u_a", "c_2")
    assert not await storage.calls.delete("u_a", "c_2")
    assert not await storage.calls.delete("u_a", "c_9")  # bob's
    assert await storage.calls.entries("u_a", "c_2") == []
    assert await ids(storage, terms=[["bread"]]) == []
    assert await storage.calls.delete_all("u_a", "ag_1") == 2
    assert await ids(storage) == []
    assert await storage.calls.get("u_b", "c_9") is not None
    assert await storage.calls.delete_all("u_b") == 1
    assert await storage.calls.list("u_b") == []


async def test_expiry_is_set_capped_and_purged(storage):
    await history(storage)  # alice: c_1 (ag_1, t=10), c_2 (ag_2, t=20), c_3 (ag_1, t=30)
    assert await storage.calls.set_expiry("u_a", "ag_1", 100.0) == 2
    assert [(await storage.calls.get("u_a", c)).expires_at for c in ("c_1", "c_2", "c_3")] == [110.0, None, 130.0]
    assert await storage.calls.cap_expiry(50.0) == 4  # c_1, c_3 longer; c_2 and bob's c_9 kept forever
    assert [(await storage.calls.get("u_a", c)).expires_at for c in ("c_1", "c_2", "c_3")] == [60.0, 70.0, 80.0]
    await storage.calls.create(call(id="c_open", created_at=1.0, expires_at=2.0))  # still recording
    assert await storage.calls.purge_expired(70.0) == 3  # c_1, c_2 and bob's c_9 (65)
    assert await ids(storage) == ["c_3", "c_open"]
    assert await storage.calls.list("u_b") == []
    assert await ids(storage, terms=[["milk"]]) == []
    assert await storage.calls.set_expiry("u_a", "ag_1", None) == 2  # c_3 and c_open
    assert (await storage.calls.get("u_a", "c_3")).expires_at is None


async def test_entries_are_resealed_in_place(storage):
    await history(storage)
    plain = await storage.calls.entries_by_seal(False, 10)
    assert sorted((e.call_id, e.seq) for e in plain) == [("c_1", 0), ("c_2", 0), ("c_2", 1), ("c_9", 0)]
    assert len(await storage.calls.entries_by_seal(False, 2)) == 2
    first = [e for e in plain if (e.call_id, e.seq) == ("c_1", 0)][0]
    assert await storage.calls.replace_entry(replace(first, text="sealed!", sealed=True), ["xabc"])
    assert not await storage.calls.replace_entry(replace(first, call_id="c_gone"), [])
    assert [(e.call_id, e.text) for e in await storage.calls.entries_by_seal(True, 10)] == [("c_1", "sealed!")]
    assert await ids(storage, terms=[["xabc"]]) == ["c_1"]
    assert await ids(storage, terms=[["buy"]]) == []  # the old terms are gone


async def test_meta_delete(storage):
    await storage.meta.set("k", "1")
    await storage.meta.delete("k")
    await storage.meta.delete("k")
    assert await storage.meta.get("k") is None


async def test_deleting_a_user_deletes_their_calls(storage):
    await history(storage)
    await storage.users.delete("u_a")
    assert await storage.calls.get("u_a", "c_1") is None
    assert await storage.calls.entries("u_a", "c_1") == []
    assert [c.id for c in await storage.calls.list("u_b", terms=[["milk"]])] == ["c_9"]


async def test_sqlite_index_forgets_deleted_text():
    st = open_sqlite_storage(":memory:")
    await history(st)
    await st.users.delete("u_a")
    await st.calls.delete("u_b", "c_9")
    assert st.db.query("SELECT COUNT(*) FROM history_fts")[0][0] == 0
    assert st.db.query("SELECT COUNT(*) FROM call_entries")[0][0] == 0


async def test_link_central_subject(storage):
    u = await storage.users.create("u_000000000001", "ana", "Ana", 1.0)
    assert await storage.users.link_central(u.id, "https://i#sub-1")
    assert (await storage.users.by_central("https://i#sub-1")).id == u.id
    assert (await storage.users.get(u.id)).central_subject == "https://i#sub-1"
    assert await storage.users.link_central(u.id, "https://i#sub-1")  # idempotent
    assert await storage.users.link_central("u_missing00000", "https://i#x") is False


async def test_central_subject_is_unique(storage):
    a = await storage.users.create("u_000000000001", "ana", "Ana", 1.0)
    b = await storage.users.create("u_000000000002", "bia", "Bia", 1.0)
    await storage.users.link_central(a.id, "https://i#sub-1")
    with pytest.raises(Conflict):
        await storage.users.link_central(b.id, "https://i#sub-1")
    assert (await storage.users.get(b.id)).central_subject is None


async def test_relink_replaces_and_unlink_clears(storage):
    u = await storage.users.create("u_000000000001", "ana", "Ana", 1.0)
    await storage.users.link_central(u.id, "https://i#old")
    await storage.users.link_central(u.id, "https://i#new")
    assert await storage.users.by_central("https://i#old") is None
    assert await storage.users.unlink_central(u.id) is True
    assert await storage.users.unlink_central(u.id) is False
    assert await storage.users.by_central("https://i#new") is None
    assert (await storage.users.get(u.id)).central_subject is None


async def test_targeted_requests(storage):
    u = await storage.users.create("u_000000000001", "ana", "Ana", 1.0)
    await storage.pairing.add_request("h1", "1234", "watch", 10.0, 100.0, target_user_id=u.id)
    await storage.pairing.add_request("h2", "5678", "other", 10.0, 100.0)
    found = await storage.pairing.pending_for(u.id, 20.0)
    assert [r.poll_hash for r in found] == ["h1"] and found[0].target_user_id == u.id
    assert (await storage.pairing.get_request("h2", 20.0)).target_user_id is None
    assert await storage.pairing.pending_for(u.id, 200.0) == []  # expired


async def test_pending_for_is_oldest_first_and_only_pending(storage):
    u = await storage.users.create("u_000000000001", "ana", "Ana", 1.0)
    await storage.pairing.add_request("h2", "0002", "second", 12.0, 100.0, target_user_id=u.id)
    await storage.pairing.add_request("h1", "0001", "first", 11.0, 100.0, target_user_id=u.id)
    await storage.pairing.add_request("h3", "0003", "denied", 13.0, 100.0, target_user_id=u.id)
    await storage.pairing.deny("h3")
    assert [r.poll_hash for r in await storage.pairing.pending_for(u.id, 20.0)] == ["h1", "h2"]


async def test_deny_request(storage):
    await storage.pairing.add_request("h1", "1234", "watch", 10.0, 100.0)
    assert await storage.pairing.deny("h1") is True
    assert (await storage.pairing.get_request("h1", 20.0)).status == "denied"
    assert await storage.pairing.deny("h1") is False
    assert await storage.pairing.pending_by_id("1234", 20.0) == []


async def test_deleting_user_drops_targeted_requests(storage):
    u = await storage.users.create("u_000000000001", "ana", "Ana", 1.0)
    await storage.pairing.add_request("h1", "1234", "watch", 10.0, 100.0, target_user_id=u.id)
    await storage.users.delete(u.id)
    assert await storage.pairing.get_request("h1", 20.0) is None
