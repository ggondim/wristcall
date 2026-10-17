"""Single-use long link codes: POST /v1/account/link takes them next to the 8-digit pairing code."""

import base64
import re
import time

import pytest

from conftest import fake_config
from test_account import api_token, client, issuer, link, linked_user, new_code, router  # noqa: F401  (fixtures)
from test_api import run
from test_pair_account import account_config
from wristcall.link import (
    LINK_TTL_S, LinkInvite, LinkService, link_url, new_link_code, render_invite, startup_invite, target_user,
)
from wristcall.pairing import hash_secret
from wristcall.storage import User, open_sqlite_storage
from wristcall.users import UserError, UserService


def issue(client, *, expires_in: float = LINK_TTL_S) -> str:
    """Puts a link code for the owner straight into the storage, as the CLI and the startup QR will."""
    api_token(client)  # creates the owner
    storage = client.app.state.storage
    owner = run(storage.users.by_handle("owner"))
    code = new_link_code()
    run(storage.pairing.add_link_code(hash_secret(code), owner.id, time.time() + expires_in))
    return code


def test_new_link_code_shape():
    codes = {new_link_code() for _ in range(50)}
    assert len(codes) == 50
    assert all(re.fullmatch(r"[A-Z2-7]{26}", c) for c in codes)


def test_link_with_link_code_returns_api_token(client, issuer):
    code = issue(client)
    r = link(client, issuer.token(), code=code)
    assert r.status_code == 200
    body = r.json()
    owner = run(client.app.state.storage.users.by_handle("owner"))
    assert body["linked"] is True and body["user"]["handle"] == "owner"
    assert body["api_token"].startswith("wc_pat_")
    assert linked_user(client).id == owner.id


def test_link_code_is_single_use(client, issuer):
    code = issue(client)
    assert link(client, issuer.token(), code=code).status_code == 200
    again = link(client, issuer.token(), code=code)
    assert again.status_code == 401 and again.json()["error"] == "invalid_code"
    assert linked_user(client) is not None


def test_stale_link_code_is_refused_after_linking_with_the_newest(client, issuer):
    # Two startups of a new server: each prints a code. Linking with the newest kills the earlier one,
    # so a code left in the log cannot re-link the owner to another account.
    api_token(client)  # creates the owner
    storage = client.app.state.storage
    owner = run(storage.users.by_handle("owner"))
    stale = issue(client)
    links = LinkService(storage, "https://testserver")
    newest = run(links.issue(owner.id)).code
    assert link(client, issuer.token(), code=newest).status_code == 200
    r = link(client, issuer.token(sub="someone-else"), code=stale)
    assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    assert linked_user(client).id == owner.id


def test_claiming_a_link_code_drops_the_users_other_codes(client, issuer):
    first, second = issue(client), issue(client)  # added straight to the storage, side by side
    assert link(client, issuer.token(), code=second).status_code == 200
    r = link(client, issuer.token(sub="someone-else"), code=first)
    assert r.status_code == 401 and r.json()["error"] == "invalid_code"


def test_link_code_expires(client, issuer):
    code = issue(client, expires_in=-1)
    r = link(client, issuer.token(), code=code)
    assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    assert linked_user(client) is None


def test_bad_account_token_does_not_spend_the_link_code(client, issuer):
    code = issue(client)
    bad = link(client, issuer.token(aud=["other"]), code=code)
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_account_token"
    good = link(client, issuer.token(), code=code)
    assert good.status_code == 200 and good.json()["api_token"].startswith("wc_pat_")


def test_link_code_accepts_lowercase_and_separators(client, issuer):
    code = issue(client)
    groups = [code.lower()[i : i + 4] for i in range(0, 26, 4)]
    assert link(client, issuer.token(), code=f"  {'-'.join(groups)}  ").status_code == 200


def test_wrong_link_code_does_not_burn_pairing_codes(client, issuer):
    pairing = new_code(client, api_token(client))
    for _ in range(5):
        r = link(client, issuer.token(), code=new_link_code())
        assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    assert link(client, issuer.token(), code=pairing).status_code == 200


def test_link_log_names_the_kind_never_the_code(client, issuer, caplog):
    code = issue(client)
    with caplog.at_level("INFO", logger="wristcall.api"):
        assert link(client, issuer.token(), code=code).status_code == 200
    assert "(link code)" in caplog.text and code not in caplog.text


# ---------- link URL, QR and startup ----------

CODE = "K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
STARTUP_AGAIN = "docker compose exec wristcall wristcall link"


def link_codes(st) -> int:
    return st.db.query("SELECT COUNT(*) FROM link_codes")[0][0]


def codes_in(text: str) -> list[str]:
    return re.findall(r"/link#v=1&s=[^&\s]+&c=([A-Z2-7]{26})", text)


@pytest.fixture
async def st():
    s = open_sqlite_storage(":memory:")
    yield s
    await s.close()


def central(**server):
    return account_config("attestation", public_url="https://testserver", **server)


async def startup(st, cfg, links=None):
    if links is None and cfg.central_account:
        links = LinkService(st, cfg.server.public_url)
    return await startup_invite(st, cfg, UserService(st), links)


def test_link_url_format():
    assert link_url("https://home.example.com", CODE) == (
        "https://wristcall-app.trigram.com.br/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
    )
    assert link_url("https://h.example:8443/wc", CODE).endswith("#v=1&s=https://h.example:8443/wc&c=" + CODE)
    assert "s=https://h.example/a%3Fb&" in link_url("https://h.example/a?b", CODE)
    assert "s=https://h.example/a%20b&" in link_url("https://h.example/a b", CODE)


def test_new_link_code_is_16_random_bytes_in_base32():
    assert len(base64.b32decode(new_link_code() + "======")) == 16


def test_render_invite_has_qr_and_copy():
    user = User("u_1", "owner", "Owner", 1.0)
    url = link_url("https://home.example.com", CODE)
    text = render_invite(LinkInvite(user, CODE, url, time.time() + LINK_TTL_S), again="wristcall link")
    lines = text.splitlines()
    for line in (
        "Connect this server to your iPhone",
        "Scan this QR code with your iPhone camera:",
        "Or open this link on your iPhone:",
        url,
        "Works once, for 15 minutes. New code: wristcall link",
    ):
        assert line in lines
    assert len([ln for ln in lines if any(c in ln for c in "█▀▄")]) >= 20
    assert "For user" not in text
    with_user = render_invite(LinkInvite(user, CODE, url, 0.0), again="wristcall link", user_line=True)
    assert "For user owner." in with_user.splitlines()


async def test_startup_prints_qr_on_a_new_server(st):
    user = await UserService(st).create("ana")
    first, second = await startup(st, central()), await startup(st, central())
    assert "Connect this server to your iPhone" in first and STARTUP_AGAIN in first
    (c1,), (c2,) = codes_in(first), codes_in(second)
    assert c1 != c2 and "https://wristcall-app.trigram.com.br/link#v=1&s=https://testserver&c=" in first
    assert await st.pairing.claim_link_code(hash_secret(c1), time.time()) is None  # replaced by the restart's code
    assert await st.pairing.claim_link_code(hash_secret(c2), time.time()) == user.id


@pytest.mark.parametrize("in_use", ["token", "device"])
async def test_startup_has_no_code_when_the_owner_is_in_use(st, in_use):
    users = UserService(st)
    user = await users.create("owner")
    if in_use == "token":
        await users.issue_token(user.id, "cli")
    else:
        await st.devices.create("d_1", user.id, "Watch", hash_secret("device-token"), time.time())
    text = await startup(st, central())
    assert "wristcall link" in text and "link#" not in text and "--user" not in text
    assert link_codes(st) == 0


async def test_startup_audience_mismatch_does_nothing(st):
    assert await startup(st, account_config("attestation", public_url="https://elsewhere.example.com")) is None
    assert await startup(st, account_config("attestation")) is None  # http://testserver is no audience at all
    assert await st.users.list() == [] and link_codes(st) == 0


async def test_startup_creates_owner_when_there_are_no_users(st):
    text = await startup(st, central())
    (owner,) = await st.users.list()
    assert (owner.handle, owner.display_name) == ("owner", "Owner")
    (code,) = codes_in(text)
    assert await st.pairing.claim_link_code(hash_secret(code), time.time()) == owner.id


async def test_startup_is_silent_once_a_user_is_linked(st):
    users = UserService(st)
    ana = await users.create("ana")
    bob = await users.create("bob")
    await st.users.link_central(bob.id, "https://issuer.test#sub-1", 1.0)
    assert await startup(st, central()) is None
    await st.users.delete(ana.id)
    assert await startup(st, central()) is None
    assert link_codes(st) == 0


async def test_startup_hints_with_several_users(st):
    users = UserService(st)
    await users.create("ana")
    await users.create("bob")
    text = await startup(st, central())
    assert "wristcall link --user <handle>" in text and "link#" not in text
    assert link_codes(st) == 0


async def test_startup_hints_without_central_account(st):
    text = await startup(st, fake_config())
    assert "central_account" in text and "README" in text and "link#" not in text
    assert await st.users.list() == [] and link_codes(st) == 0
    users = UserService(st)
    owner = await users.create("owner")
    assert "central_account" in await startup(st, fake_config())
    await users.issue_token(owner.id, "cli")
    assert await startup(st, fake_config()) is None


async def test_target_user(st):
    users = UserService(st)
    owner = await target_user(st, users, None)
    assert owner.handle == "owner" and await target_user(st, users, None) == owner
    bob = await users.create("bob")
    assert await target_user(st, users, "bob") == bob
    with pytest.raises(UserError, match="several users"):
        await target_user(st, users, None)
    with pytest.raises(UserError, match="not found"):
        await target_user(st, users, "zed")


async def test_link_service_purges_and_stores_only_the_hash(st):
    user = await UserService(st).create("ana")
    clock = [1000.0]
    links = LinkService(st, "https://testserver", now=lambda: clock[0])
    old = await links.issue(user.id)
    assert old.expires_at == 1000.0 + LINK_TTL_S and old.url == link_url("https://testserver", old.code)
    assert old.user == user and link_codes(st) == 1
    assert st.db.query("SELECT code_hash FROM link_codes")[0][0] == hash_secret(old.code)
    clock[0] += LINK_TTL_S + 1
    await links.issue(user.id)
    assert link_codes(st) == 1  # the expired one was purged first


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
async def test_issuing_again_leaves_only_the_newest_code(kind):
    from memory_storage import MemoryStorage

    store = open_sqlite_storage(":memory:") if kind == "sqlite" else MemoryStorage()
    try:
        users = UserService(store)
        ana, bob = await users.create("ana"), await users.create("bob")
        links = LinkService(store, "https://testserver")
        bobs = await links.issue(bob.id)
        first, second = await links.issue(ana.id), await links.issue(ana.id)
        now = time.time()
        assert await store.pairing.claim_link_code(hash_secret(first.code), now) is None
        assert await store.pairing.claim_link_code(hash_secret(second.code), now) == ana.id
        assert await store.pairing.claim_link_code(hash_secret(bobs.code), now) == bob.id  # other users keep theirs
    finally:
        await store.close()
