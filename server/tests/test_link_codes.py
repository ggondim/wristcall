"""Single-use long link codes: POST /v1/account/link takes them next to the 8-digit pairing code."""

import re
import time

from test_account import api_token, client, issuer, link, linked_user, new_code, router  # noqa: F401  (fixtures)
from test_api import run
from wristcall.link import LINK_TTL_S, new_link_code
from wristcall.pairing import hash_secret


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
