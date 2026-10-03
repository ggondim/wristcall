import pytest

from wristcall.pairing import (
    Paired,
    PairingDenied,
    PairingGone,
    PairingService,
    Pending,
    NotFound,
    format_code,
    hash_secret,
    normalize_code,
)
from wristcall.ratelimit import RateLimiter
from wristcall.store import Database


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def service(approval="code", db=None):
    clock = Clock()
    return PairingService(db or Database(":memory:"), approval, now=clock), clock


def other_code(code: str) -> str:
    return f"{(int(code) + 1) % 10**8:08d}"


def test_code_helpers():
    assert format_code("12345678") == "1234 5678"
    assert normalize_code(" 1234-5678 ") == "12345678"
    assert normalize_code("123") is None
    assert len(hash_secret("x")) == 64


def test_flow_a_pairs_and_authenticates():
    s, _ = service()
    c = s.create_code()
    assert len(c.code) == 8 and c.code.isdigit()
    r = s.pair(format_code(c.code), "Apple Watch")
    assert isinstance(r, Paired)
    dev = s.authenticate(r.token)
    assert dev is not None and dev.name == "Apple Watch" and dev.id == r.device_id
    assert s.authenticate("wrong-token") is None
    stored = s._db.query("SELECT token_hash FROM devices")[0]["token_hash"]
    assert stored == hash_secret(r.token) and stored != r.token


def test_code_is_single_use():
    s, _ = service()
    c = s.create_code()
    s.pair(c.code, "one")
    with pytest.raises(PairingDenied):
        s.pair(c.code, "two")


def test_discarded_code_cannot_be_used():
    s, _ = service()
    c = s.create_code()
    s.discard_code(c.code)
    with pytest.raises(PairingDenied):
        s.pair(c.code, "x")


def test_code_expires():
    s, clock = service()
    c = s.create_code()
    clock.advance(601)
    with pytest.raises(PairingDenied):
        s.pair(c.code, "late")


def test_five_wrong_attempts_kill_the_active_code():
    s, _ = service()
    c = s.create_code()
    for _ in range(5):
        with pytest.raises(PairingDenied):
            s.pair(other_code(c.code), "attacker")
    with pytest.raises(PairingDenied):
        s.pair(c.code, "owner")


def test_code_mode_rejects_unknown_code_without_creating_requests():
    s, _ = service("code")
    with pytest.raises(PairingDenied):
        s.pair("12345678", "x")
    with pytest.raises(PairingDenied):
        s.pair(None, "x")
    assert s.list_pending() == []


def test_flow_b_pending_approval_and_single_delivery():
    s, _ = service("manual")
    p = s.pair("99999999", "My Apple Watch")
    assert isinstance(p, Pending)
    assert len(p.request_id) == 4 and p.request_id.isdigit()
    assert isinstance(s.poll(p.poll_token), Pending)
    assert [r.request_id for r in s.list_pending()] == [p.request_id]
    assert s.approve(p.request_id) == "My Apple Watch"
    r = s.poll(p.poll_token)
    assert isinstance(r, Paired) and s.authenticate(r.token).name == "My Apple Watch"
    with pytest.raises(PairingGone):
        s.poll(p.poll_token)


def test_short_request_id_cannot_be_used_to_poll():
    s, _ = service("manual")
    p = s.pair(None, "Watch")
    s.approve(p.request_id)
    with pytest.raises(PairingGone):
        s.poll(p.request_id)
    assert isinstance(s.poll(p.poll_token), Paired)


def test_pending_request_expires():
    s, clock = service("manual")
    p = s.pair(None, "Watch")
    clock.advance(601)
    with pytest.raises(PairingGone):
        s.poll(p.poll_token)
    with pytest.raises(NotFound):
        s.approve(p.request_id)


def test_revoke():
    s, _ = service()
    r = s.pair(s.create_code().code, "Watch")
    assert [d.id for d in s.list_devices()] == [r.device_id]
    assert s.revoke(r.device_id) is True
    assert s.authenticate(r.token) is None
    assert s.revoke(r.device_id) is False
    assert s.list_devices() == []


def test_same_database_file_shared_by_cli_and_server(tmp_path):
    cli, _ = service(db=Database(tmp_path / "wristcall.db"))
    server, _ = service(db=Database(tmp_path / "wristcall.db"))
    c = cli.create_code()
    assert isinstance(server.pair(c.code, "Watch"), Paired)


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


def test_device_name_control_characters_are_stripped():
    s, _ = service("manual")
    s.pair(None, "Watch\x1b[2J")
    assert [r.device_name for r in s.list_pending()] == ["Watch[2J"]


def test_approve_after_delivery_is_not_found():
    s, _ = service("manual")
    p = s.pair(None, "Watch")
    s.approve(p.request_id)
    assert isinstance(s.poll(p.poll_token), Paired)
    with pytest.raises(NotFound):
        s.approve(p.request_id)
    with pytest.raises(PairingGone):
        s.poll(p.poll_token)
    assert len(s.list_devices()) == 1


def test_approve_refuses_colliding_short_ids():
    s, clock = service("manual")
    for poll_hash in ("hash-a", "hash-b"):
        s._db.execute(
            "INSERT INTO pairing_requests (poll_hash, short_id, device_name, created_at, expires_at, status) "
            "VALUES (?, '1234', 'Watch', ?, ?, 'pending')",
            (poll_hash, clock(), clock() + 600),
        )
    with pytest.raises(NotFound):
        s.approve("1234")
    statuses = [r["status"] for r in s._db.query("SELECT status FROM pairing_requests")]
    assert statuses == ["pending", "pending"]
