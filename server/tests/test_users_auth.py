import pytest

from wristcall.auth import Authenticator, bearer
from wristcall.pairing import hash_secret
from wristcall.storage import open_sqlite_storage
from wristcall.users import API_TOKEN_PREFIX, UserError, UserService


@pytest.fixture
async def st():
    s = open_sqlite_storage(":memory:")
    yield s
    await s.close()


async def test_create_and_resolve(st):
    users = UserService(st, now=lambda: 5.0)
    with pytest.raises(UserError, match="no users yet"):
        await users.resolve(None)
    alice = await users.create("alice", "Alice A.")
    assert alice.id.startswith("u_") and alice.display_name == "Alice A." and alice.created_at == 5.0
    assert (await users.resolve(None)).id == alice.id
    await users.create("bob")
    with pytest.raises(UserError, match="--user"):
        await users.resolve(None)
    assert (await users.resolve("bob")).display_name == "bob"
    with pytest.raises(UserError, match="not found"):
        await users.resolve("carol")


@pytest.mark.parametrize("handle", ["Alice", "a b", "-a", "", "a" * 33, "ação", "alice\n"])
async def test_invalid_handles(st, handle):
    with pytest.raises(UserError, match="handle"):
        await UserService(st).create(handle)
    await UserService(st).create("valid")
    with pytest.raises(UserError, match="handle"):
        await UserService(st).rename("valid", handle)


async def test_duplicate_handle(st):
    users = UserService(st)
    await users.create("alice")
    with pytest.raises(UserError, match="already exists"):
        await users.create("alice")


async def test_rename_and_delete(st):
    users = UserService(st)
    await users.create("owner")
    await users.create("bob")
    renamed = await users.rename("owner", "gustavo", "Gustavo")
    assert (renamed.handle, renamed.display_name) == ("gustavo", "Gustavo")
    with pytest.raises(UserError, match="already exists"):
        await users.rename("gustavo", "bob")
    with pytest.raises(UserError, match="handle"):
        await users.rename("gustavo", "Bad Handle")
    assert (await users.delete("bob")).handle == "bob"
    assert [u.handle for u in await st.users.list()] == ["gustavo"]


async def test_tokens_are_shown_once_and_stored_hashed(st):
    users = UserService(st)
    alice = await users.create("alice")
    record, token = await users.issue_token(alice.id, "laptop")
    assert token.startswith(API_TOKEN_PREFIX) and len(token) == len(API_TOKEN_PREFIX) + 43
    (row,) = st.db.query("SELECT token_hash FROM api_tokens")
    assert row["token_hash"] == hash_secret(token)
    assert [t.name for t in await users.list_tokens(alice.id)] == ["laptop"]
    assert await users.revoke_token(record.id) is True
    assert await users.list_tokens(alice.id) == []


@pytest.mark.parametrize(
    "header, token",
    [(None, None), ("", None), ("Basic abc", None), ("Bearer ", None), ("Bearer abc", "abc"), ("bearer  abc ", "abc")],
)
def test_bearer(header, token):
    assert bearer(header) == token


async def test_authenticator_tells_devices_from_api_tokens(st):
    users = UserService(st)
    alice = await users.create("alice")
    auth = Authenticator(st, now=lambda: 9.0)
    await st.devices.create("d1", alice.id, "Watch", hash_secret("device-token"), 1.0)
    device = await auth.authenticate("Bearer device-token")
    assert device.kind == "device" and device.user_id == alice.id and device.device.id == "d1"
    record, token = await users.issue_token(alice.id, "cli")
    api = await auth.authenticate(f"Bearer {token}")
    assert api.kind == "api" and api.user_id == alice.id and api.token_id == record.id and api.device is None
    assert (await st.tokens.list(alice.id))[0].last_used_at == 9.0
    assert await auth.authenticate("Bearer wc_pat_forged") is None
    assert await auth.authenticate("Bearer nope") is None
    assert await auth.authenticate(None) is None
    await users.revoke_token(record.id)
    assert await auth.authenticate(f"Bearer {token}") is None


async def test_deleting_a_user_logs_out_everything(st):
    users = UserService(st)
    alice = await users.create("alice")
    auth = Authenticator(st)
    await st.devices.create("d1", alice.id, "Watch", hash_secret("device-token"), 1.0)
    _, token = await users.issue_token(alice.id, "cli")
    await users.delete("alice")
    assert await auth.authenticate("Bearer device-token") is None
    assert await auth.authenticate(f"Bearer {token}") is None


async def test_device_without_owner_is_not_a_principal(st):
    st.db.execute("INSERT INTO devices (id, name, token_hash, created_at) VALUES ('d0', 'Old', ?, 1.0)", (hash_secret("old"),))
    assert await Authenticator(st).authenticate("Bearer old") is None


async def test_rename_trims_the_display_name(st):
    users = UserService(st)
    await users.create("alice")
    assert (await users.rename("alice", display_name="  Alice A.  ")).display_name == "Alice A."
    assert (await users.rename("alice", display_name="x" * 80)).display_name == "x" * 64
    for empty in ("", "   "):
        with pytest.raises(UserError, match="display name"):
            await users.rename("alice", display_name=empty)
    assert (await users.resolve("alice")).display_name == "x" * 64
