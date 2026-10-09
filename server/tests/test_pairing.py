import httpx
import pytest
import respx

from wristcall.directory_client import DirectoryClient, DirectoryError
from wristcall.pairing import (
    DeviceLimit,
    NotFound,
    Paired,
    PairingDenied,
    PairingGone,
    PairingService,
    Pending,
    format_code,
    hash_secret,
    issue_code,
    normalize_code,
)
from wristcall.ratelimit import RateLimiter
from wristcall.storage import open_sqlite_storage


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


async def service(approval="code", storage=None, **kw):
    st = storage or open_sqlite_storage(":memory:")
    if await st.users.get("u_a") is None:
        await st.users.create("u_a", "alice", "Alice", 1.0)
        await st.users.create("u_b", "bob", "Bob", 1.0)
    clock = Clock()
    return PairingService(st, approval, now=clock, **kw), clock


def other_code(code: str) -> str:
    return f"{(int(code) + 1) % 10**8:08d}"


def test_code_helpers():
    assert format_code("12345678") == "1234 5678"
    assert normalize_code(" 1234-5678 ") == "12345678"
    assert normalize_code("123") is None
    assert len(hash_secret("x")) == 64


async def test_flow_a_pairs_to_the_code_owner_and_authenticates():
    s, _ = await service()
    c = await s.create_code("u_b")
    assert len(c.code) == 8 and c.code.isdigit()
    r = await s.pair(format_code(c.code), "Apple Watch")
    assert isinstance(r, Paired)
    dev = await s.authenticate(r.token)
    assert dev is not None and dev.name == "Apple Watch" and dev.id == r.device_id and dev.user_id == "u_b"
    assert await s.authenticate("wrong-token") is None
    stored = s._st.db.query("SELECT token_hash FROM devices")[0]["token_hash"]
    assert stored == hash_secret(r.token) and stored != r.token


async def test_code_is_single_use():
    s, _ = await service()
    c = await s.create_code("u_a")
    await s.pair(c.code, "one")
    with pytest.raises(PairingDenied):
        await s.pair(c.code, "two")


async def test_discarded_code_cannot_be_used():
    s, _ = await service()
    c = await s.create_code("u_a")
    await s.discard_code(c.code)
    with pytest.raises(PairingDenied):
        await s.pair(c.code, "x")


async def test_code_expires():
    s, clock = await service()
    c = await s.create_code("u_a")
    clock.advance(601)
    with pytest.raises(PairingDenied):
        await s.pair(c.code, "late")


async def test_five_wrong_attempts_kill_the_active_code():
    s, _ = await service()
    c = await s.create_code("u_a")
    for _ in range(5):
        with pytest.raises(PairingDenied):
            await s.pair(other_code(c.code), "attacker")
    with pytest.raises(PairingDenied):
        await s.pair(c.code, "owner")


async def test_code_mode_rejects_unknown_code_without_creating_requests():
    s, _ = await service("code")
    with pytest.raises(PairingDenied):
        await s.pair("12345678", "x")
    with pytest.raises(PairingDenied):
        await s.pair(None, "x")
    assert await s.list_pending() == []


async def test_flow_b_pending_approval_and_single_delivery():
    s, _ = await service("manual")
    p = await s.pair("99999999", "My Apple Watch")
    assert isinstance(p, Pending)
    assert len(p.request_id) == 4 and p.request_id.isdigit()
    assert isinstance(await s.poll(p.poll_token), Pending)
    assert [r.request_id for r in await s.list_pending()] == [p.request_id]
    assert await s.approve(p.request_id, "u_b") == "My Apple Watch"
    r = await s.poll(p.poll_token)
    assert isinstance(r, Paired)
    dev = await s.authenticate(r.token)
    assert dev.name == "My Apple Watch" and dev.user_id == "u_b"
    with pytest.raises(PairingGone):
        await s.poll(p.poll_token)


async def test_short_request_id_cannot_be_used_to_poll():
    s, _ = await service("manual")
    p = await s.pair(None, "Watch")
    await s.approve(p.request_id, "u_a")
    with pytest.raises(PairingGone):
        await s.poll(p.request_id)
    assert isinstance(await s.poll(p.poll_token), Paired)


async def test_pending_request_expires():
    s, clock = await service("manual")
    p = await s.pair(None, "Watch")
    clock.advance(601)
    with pytest.raises(PairingGone):
        await s.poll(p.poll_token)
    with pytest.raises(NotFound):
        await s.approve(p.request_id, "u_a")


async def test_revoke_and_ownership():
    s, _ = await service()
    r = await s.pair((await s.create_code("u_a")).code, "Watch")
    assert [d.id for d in await s.list_devices()] == [r.device_id]
    assert [d.id for d in await s.list_devices("u_b")] == []
    assert await s.revoke(r.device_id, user_id="u_b") is False
    assert await s.revoke(r.device_id) is True
    assert await s.authenticate(r.token) is None
    assert await s.revoke(r.device_id) is False
    assert await s.list_devices() == []


async def test_same_database_file_shared_by_cli_and_server(tmp_path):
    cli, _ = await service(storage=open_sqlite_storage(tmp_path))
    server, _ = await service(storage=open_sqlite_storage(tmp_path))
    c = await cli.create_code("u_a")
    assert isinstance(await server.pair(c.code, "Watch"), Paired)


async def test_device_without_owner_is_not_authenticated():
    s, _ = await service()
    s._st.db.execute("INSERT INTO devices (id, name, token_hash, created_at) VALUES ('d0', 'Old', ?, 1.0)", (hash_secret("old"),))
    assert await s.authenticate("old") is None


async def test_device_limit_per_user():
    s, _ = await service(max_devices_per_user=1)
    await s.pair((await s.create_code("u_a")).code, "Watch 1")
    with pytest.raises(DeviceLimit):
        await s.create_code("u_a")
    assert isinstance(await s.pair((await s.create_code("u_b")).code, "Bob's"), Paired)


async def test_device_limit_on_manual_approval():
    s, _ = await service("manual", max_devices_per_user=1)
    p1 = await s.pair(None, "Watch 1")
    p2 = await s.pair(None, "Watch 2")
    await s.approve(p1.request_id, "u_a")
    await s.poll(p1.poll_token)
    with pytest.raises(DeviceLimit):
        await s.approve(p2.request_id, "u_a")


def test_rate_limiter():
    clock = Clock()
    rl = RateLimiter(limit=2, window_s=60, now=clock)
    assert rl.allow("1.2.3.4") and rl.allow("1.2.3.4")
    assert not rl.allow("1.2.3.4")
    assert rl.allow("5.6.7.8")
    clock.advance(61)
    assert rl.allow("1.2.3.4")


def test_normalize_code_accepts_only_ascii_digits():
    assert normalize_code(" 1234-5678 ") == "12345678"
    assert normalize_code("１２３４-５６７８") is None
    assert normalize_code("12ab34-5678") is None


async def test_device_name_control_characters_are_stripped():
    s, _ = await service("manual")
    await s.pair(None, "Watch\x1b[2J")
    assert [r.device_name for r in await s.list_pending()] == ["Watch[2J"]


async def test_approve_after_delivery_is_not_found():
    s, _ = await service("manual")
    p = await s.pair(None, "Watch")
    await s.approve(p.request_id, "u_a")
    assert isinstance(await s.poll(p.poll_token), Paired)
    with pytest.raises(NotFound):
        await s.approve(p.request_id, "u_a")
    with pytest.raises(PairingGone):
        await s.poll(p.poll_token)
    assert len(await s.list_devices()) == 1


async def test_approve_refuses_colliding_short_ids():
    s, clock = await service("manual")
    for poll_hash in ("hash-a", "hash-b"):
        s._st.db.execute(
            "INSERT INTO pairing_requests (poll_hash, short_id, device_name, created_at, expires_at, status) "
            "VALUES (?, '1234', 'Watch', ?, ?, 'pending')",
            (poll_hash, clock(), clock() + 600),
        )
    with pytest.raises(NotFound):
        await s.approve("1234", "u_a")
    statuses = [r["status"] for r in s._st.db.query("SELECT status FROM pairing_requests")]
    assert statuses == ["pending", "pending"]


async def test_issue_code_without_directory():
    s, _ = await service()
    issued = await issue_code(s, "u_a", "https://wc.test", None)
    assert issued.via_directory is False and issued.warning is None
    assert isinstance(await s.pair(issued.code.code, "Watch"), Paired)


@respx.mock
async def test_issue_code_registers_and_retries_on_conflict():
    route = respx.post("https://dir.test/v1/codes").mock(
        side_effect=[httpx.Response(409, json={"error": "conflict"}), httpx.Response(201, json={})]
    )
    s, _ = await service()
    issued = await issue_code(s, "u_a", "https://wc.test", DirectoryClient("https://dir.test"))
    assert issued.via_directory is True and route.call_count == 2
    first = route.calls[0].request.content
    assert issued.code.code.encode() not in first  # the conflicting code was replaced
    assert isinstance(await s.pair(issued.code.code, "Watch"), Paired)


@respx.mock
async def test_issue_code_gives_up_after_three_conflicts():
    respx.post("https://dir.test/v1/codes").mock(return_value=httpx.Response(409, json={"error": "conflict"}))
    s, _ = await service()
    with pytest.raises(DirectoryError, match="3 codes"):
        await issue_code(s, "u_a", "https://wc.test", DirectoryClient("https://dir.test"))
    assert s._st.db.query("SELECT COUNT(*) FROM pairing_codes")[0][0] == 0


@respx.mock
async def test_issue_code_with_unreachable_directory_still_returns_a_code():
    respx.post("https://dir.test/v1/codes").mock(side_effect=httpx.ConnectError("down"))
    s, _ = await service()
    issued = await issue_code(s, "u_a", "https://wc.test", DirectoryClient("https://dir.test"))
    assert issued.via_directory is False and "directory" in issued.warning
    assert isinstance(await s.pair(issued.code.code, "Watch"), Paired)


# ---------- pairing through the central account (mechanisms A and B) ----------


@pytest.fixture
async def svc():
    return (await service())[0]


@pytest.fixture
async def svc_manual():
    return (await service("manual"))[0]


@pytest.fixture
async def user(svc):
    return await svc._st.users.get("u_a")


@pytest.fixture
async def other_user(svc):
    return await svc._st.users.get("u_b")


async def test_pair_for_user_attestation_creates_device(svc, user):
    r = await svc.pair_for_user(user.id, "Watch\x1b[2J", approval=False)
    assert isinstance(r, Paired)
    dev = await svc.authenticate(r.token)
    assert dev.user_id == user.id and dev.name == "Watch[2J"
    assert await svc.list_pending() == []


async def test_pair_for_user_approval_creates_targeted_request(svc, user):
    p = await svc.pair_for_user(user.id, "  ", approval=True)
    assert isinstance(p, Pending)
    assert isinstance(await svc.poll(p.poll_token), Pending)
    assert [(r.request_id, r.device_name) for r in await svc.pending_for(user.id)] == [(p.request_id, "watch")]
    assert await svc.approve(p.request_id, user.id, targeted_only=True) == "watch"
    paired = await svc.poll(p.poll_token)
    assert isinstance(paired, Paired)
    assert (await svc.authenticate(paired.token)).user_id == user.id
    assert await svc.pending_for(user.id) == []


async def test_cannot_approve_someone_elses_request(svc, user, other_user):
    pending = await svc.pair_for_user(user.id, "watch", approval=True)
    with pytest.raises(NotFound):
        await svc.approve(pending.request_id, other_user.id)
    with pytest.raises(NotFound):
        await svc.approve(pending.request_id, other_user.id, targeted_only=True)
    with pytest.raises(NotFound):
        await svc.deny(pending.request_id, other_user.id)
    assert await svc.pending_for(other_user.id) == []
    assert isinstance(await svc.poll(pending.poll_token), Pending)


async def test_denied_request_is_gone_for_the_device(svc, user):
    p = await svc.pair_for_user(user.id, "Watch", approval=True)
    assert await svc.deny(p.request_id, user.id) == "Watch"
    with pytest.raises(PairingGone, match="denied"):
        await svc.poll(p.poll_token)
    with pytest.raises(NotFound):
        await svc.approve(p.request_id, user.id)
    with pytest.raises(NotFound):
        await svc.deny(p.request_id, user.id)


async def test_operator_denies_any_pending_request(svc_manual, user):
    anonymous = await svc_manual.pair(None, "Anon")
    targeted = await svc_manual.pair_for_user(user.id, "Mine", approval=True)
    assert await svc_manual.deny(anonymous.request_id, None) == "Anon"
    assert await svc_manual.deny(targeted.request_id, None) == "Mine"
    for p in (anonymous, targeted):
        with pytest.raises(PairingGone):
            await svc_manual.poll(p.poll_token)


async def test_user_cannot_deny_untargeted_request(svc_manual, user):
    p = await svc_manual.pair(None, "Anon")
    with pytest.raises(NotFound):
        await svc_manual.deny(p.request_id, user.id)
    assert isinstance(await svc_manual.poll(p.poll_token), Pending)


async def test_targeted_pending_cap(svc, user, other_user):
    for _ in range(5):
        await svc.pair_for_user(user.id, "watch", approval=True)
    with pytest.raises(PairingDenied):
        await svc.pair_for_user(user.id, "watch", approval=True)
    # Someone else's queue is untouched.
    assert isinstance(await svc.pair_for_user(other_user.id, "watch", approval=True), Pending)


async def test_targeted_requests_do_not_count_against_the_global_cap(svc_manual, user):
    for _ in range(20):
        await svc_manual.pair(None, "anon")
    with pytest.raises(PairingDenied):
        await svc_manual.pair(None, "anon")
    p = await svc_manual.pair_for_user(user.id, "mine", approval=True)
    assert isinstance(p, Pending)
    # ... and the targeted request does not use up an anonymous slot either.
    assert p.request_id not in {r.request_id for r in await svc_manual.list_pending() if r.device_name == "anon"}


async def test_pair_for_user_checks_the_device_limit_early():
    s, _ = await service(max_devices_per_user=1)
    await s.pair_for_user("u_a", "Watch 1", approval=False)
    with pytest.raises(DeviceLimit):
        await s.pair_for_user("u_a", "Watch 2", approval=False)
    with pytest.raises(DeviceLimit):
        await s.pair_for_user("u_a", "Watch 2", approval=True)
    assert await s.pending_for("u_a") == []


async def test_cli_style_approve_still_takes_untargeted(svc_manual, user):
    p = await svc_manual.pair(None, "Watch")
    with pytest.raises(NotFound):
        await svc_manual.approve(p.request_id, user.id, targeted_only=True)
    assert await svc_manual.approve(p.request_id, user.id) == "Watch"
    assert (await svc_manual.authenticate((await svc_manual.poll(p.poll_token)).token)).user_id == user.id


async def test_operator_approval_respects_the_target(svc_manual, user, other_user):
    p = await svc_manual.pair_for_user(user.id, "Watch", approval=True)
    with pytest.raises(NotFound):
        await svc_manual.approve(p.request_id, other_user.id)
    assert await svc_manual.approve(p.request_id, user.id) == "Watch"


async def test_untargeted_manual_flow_unchanged(svc_manual, user):
    p = await svc_manual.pair(None, "My Apple Watch")
    assert isinstance(p, Pending)
    assert await svc_manual.pending_for(user.id) == []
    assert [r.request_id for r in await svc_manual.list_pending()] == [p.request_id]
    assert await svc_manual.approve(p.request_id, user.id) == "My Apple Watch"
    assert isinstance(await svc_manual.poll(p.poll_token), Paired)


async def test_approve_filters_targets_before_the_collision_rule(svc, user, other_user):
    clock = svc._now
    for poll_hash, target in (("hash-a", user.id), ("hash-b", other_user.id)):
        svc._st.db.execute(
            "INSERT INTO pairing_requests (poll_hash, short_id, device_name, created_at, expires_at, status, target_user_id) "
            "VALUES (?, '1234', 'Watch', ?, ?, 'pending', ?)",
            (poll_hash, clock(), clock() + 600, target),
        )
    assert await svc.approve("1234", user.id, targeted_only=True) == "Watch"
    statuses = {r["poll_hash"]: r["status"] for r in svc._st.db.query("SELECT poll_hash, status FROM pairing_requests")}
    assert statuses == {"hash-a": "approved", "hash-b": "pending"}


async def test_device_limit_holds_when_approved_requests_are_collected_later():
    s, _ = await service(max_devices_per_user=2)
    requests = [await s.pair_for_user("u_a", f"Watch {i}", approval=True) for i in range(5)]
    for p in requests:
        await s.approve(p.request_id, "u_a", targeted_only=True)
    results = []
    for p in requests:
        try:
            results.append(await s.poll(p.poll_token))
        except DeviceLimit:
            results.append(None)
    assert sum(isinstance(r, Paired) for r in results) == 2
    assert len(await s.list_devices("u_a")) == 2
    # A refused collection is not spent: after a revoke the next poll delivers.
    await s.revoke(results[0].device_id)
    assert isinstance(await s.poll(requests[2].poll_token), Paired)
    assert len(await s.list_devices("u_a")) == 2
    with pytest.raises(DeviceLimit):
        await s.poll(requests[3].poll_token)


async def test_device_limit_holds_for_untargeted_approvals_collected_later():
    s, _ = await service("manual", max_devices_per_user=1)
    p1, p2 = await s.pair(None, "Watch 1"), await s.pair(None, "Watch 2")
    await s.approve(p1.request_id, "u_a")
    await s.approve(p2.request_id, "u_a")
    assert isinstance(await s.poll(p1.poll_token), Paired)
    with pytest.raises(DeviceLimit):
        await s.poll(p2.poll_token)
    assert len(await s.list_devices("u_a")) == 1


async def test_request_id_generation_gives_up_when_ids_run_out(monkeypatch):
    s, _ = await service("manual")
    first = await s.pair(None, "Watch")
    monkeypatch.setattr("wristcall.pairing.secrets.randbelow", lambda n: int(first.request_id))
    with pytest.raises(PairingDenied):
        await s.pair(None, "Watch")
