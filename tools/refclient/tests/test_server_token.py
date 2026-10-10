"""Per-server tokens: the refclient trades the central account login for a token made for one server (R1, R17)."""

import argparse
import json

import httpx
import pytest
import respx

from refclient import __main__ as cli
from refclient import __version__
from refclient.client import RefclientError, require_cloud, server_token

CLOUD = "https://cloud.example.com"
SERVER = "https://wc.example.com"
HEALTH = {"status": "ok", "account": {"issuer": CLOUD, "device_credential": "attestation"}}


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG", tmp_path / "refclient.json")
    monkeypatch.setattr(cli, "ACCOUNT", tmp_path / "account.json")
    (tmp_path / "account.json").write_text(json.dumps({"issuer": "https://auth.example.com", "access_token": "secret-at"}))
    return tmp_path


@respx.mock
def test_server_token_asks_the_cloud():
    route = respx.post(f"{CLOUD}/v1/server-tokens").respond(
        200, json={"token": "server-tok", "audience": SERVER, "expires_at": 1}
    )
    with httpx.Client() as http:
        assert server_token(CLOUD + "/", "secret-at", SERVER + "/", http=http) == "server-tok"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret-at"
    assert request.headers["user-agent"] == f"wristcall-refclient/{__version__}"
    # The URL as the user typed it: the Cloud normalizes it.
    assert json.loads(request.content) == {"audience": SERVER + "/"}


@respx.mock
@pytest.mark.parametrize(
    ("status", "body", "code"),
    [(422, {"error": "invalid", "message": "x"}, "invalid"), (401, {"error": "unauthorized"}, "unauthorized"),
     (404, {"error": "not_configured"}, "not_configured"), (503, "oops", "unknown")],
)
def test_server_token_errors_name_the_code_never_the_token(status, body, code):
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    respx.post(f"{CLOUD}/v1/server-tokens").respond(status, **kwargs)
    with httpx.Client() as http, pytest.raises(RefclientError) as e:
        server_token(CLOUD, "secret-at", SERVER, http=http)
    assert code in str(e.value) and str(status) in str(e.value)
    assert "secret-at" not in str(e.value)


@respx.mock
def test_server_token_needs_a_token_in_the_answer():
    respx.post(f"{CLOUD}/v1/server-tokens").respond(200, json={"audience": SERVER})
    with httpx.Client() as http, pytest.raises(RefclientError):
        server_token(CLOUD, "secret-at", SERVER, http=http)


@respx.mock
@pytest.mark.parametrize("issuer", [CLOUD, CLOUD + "/", "HTTPS://Cloud.Example.com:443"])
def test_require_cloud_accepts_the_same_url(issuer):
    respx.get(f"{SERVER}/v1/health").respond(200, json={"account": {"issuer": issuer}})
    with httpx.Client() as http:
        require_cloud(SERVER, CLOUD + "/", http=http)


@respx.mock
@pytest.mark.parametrize(
    ("health", "message"),
    [
        ({"account": {"issuer": "https://evil.test"}}, "this server trusts another central account: https://evil.test"),
        ({"account": {"issuer": "https://cloud.example.com.evil.test"}}, "another central account"),
        ({"account": None}, "this server has no central account"),
        ({"status": "ok"}, "this server has no central account"),
    ],
)
def test_require_cloud_refuses(health, message):
    respx.get(f"{SERVER}/v1/health").respond(200, json=health)
    with httpx.Client() as http, pytest.raises(RefclientError, match=message):
        require_cloud(SERVER, CLOUD, http=http)


@respx.mock
def test_pair_account_sends_the_server_token_not_the_login(account, capsys):
    respx.get(f"{SERVER}/v1/health").respond(200, json=HEALTH)
    cloud = respx.post(f"{CLOUD}/v1/server-tokens").respond(
        200, json={"token": "server-tok", "audience": SERVER, "expires_at": 1}
    )
    pair = respx.post(f"{SERVER}/v1/pair/account").respond(200, json={"device_id": "d1", "token": "secret-wct"})
    assert cli.cmd_pair_account(argparse.Namespace(server=SERVER, cloud=CLOUD, name="Mac")) == 0
    assert cloud.called and json.loads(cloud.calls.last.request.content) == {"audience": SERVER}
    sent = pair.calls.last.request
    assert json.loads(sent.content) == {"token": "server-tok", "device_name": "Mac"}
    assert b"secret-at" not in sent.content and "secret-at" not in str(sent.headers)
    assert json.loads((account / "refclient.json").read_text()) == {"server": SERVER, "token": "secret-wct"}
    out = capsys.readouterr()
    for secret in ("secret-at", "server-tok", "secret-wct"):
        assert secret not in out.out + out.err


@respx.mock
def test_pair_account_refuses_server_naming_another_cloud(account, capsys):
    respx.get(f"{SERVER}/v1/health").respond(
        200, json={"status": "ok", "account": {"issuer": "https://evil.test", "device_credential": "attestation"}}
    )
    cloud = respx.post(f"{CLOUD}/v1/server-tokens").respond(200, json={"token": "server-tok"})
    pair = respx.post(f"{SERVER}/v1/pair/account").respond(200, json={"device_id": "d1", "token": "secret-wct"})
    evil = respx.route(host="evil.test").respond(200, json={"token": "x"})
    assert cli.cmd_pair_account(argparse.Namespace(server=SERVER, cloud=CLOUD, name="Mac")) == 1
    assert not cloud.called and not pair.called and not evil.called
    err = capsys.readouterr().err
    assert "this server trusts another central account: https://evil.test" in err
    assert "secret-at" not in err


@respx.mock
def test_pair_account_reports_cloud_errors(account, capsys):
    respx.get(f"{SERVER}/v1/health").respond(200, json=HEALTH)
    respx.post(f"{CLOUD}/v1/server-tokens").respond(422, json={"error": "invalid", "message": "loopback"})
    pair = respx.post(f"{SERVER}/v1/pair/account").respond(200, json={"device_id": "d1", "token": "secret-wct"})
    assert cli.cmd_pair_account(argparse.Namespace(server=SERVER, cloud=CLOUD, name="Mac")) == 1
    assert not pair.called
    err = capsys.readouterr().err
    assert "invalid" in err and "secret-at" not in err


def test_pair_account_cli_requires_cloud(monkeypatch):
    monkeypatch.setattr("sys.argv", ["wristcall-refclient", "pair-account", "--server", SERVER])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2


@respx.mock
def test_require_cloud_does_not_print_terminal_escapes():
    respx.get(f"{SERVER}/v1/health").respond(200, json={"account": {"issuer": "https://evil.test\x1b[2J‮\x07"}})
    with httpx.Client() as http, pytest.raises(RefclientError) as e:
        require_cloud(SERVER, CLOUD, http=http)
    assert str(e.value) == "this server trusts another central account: https://evil.test[2J"


@respx.mock
def test_own_clients_are_closed(monkeypatch):
    made: list[httpx.Client] = []

    class Tracked(httpx.Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr("refclient.client.httpx.Client", Tracked)
    respx.get(f"{SERVER}/v1/health").respond(200, json=HEALTH)
    respx.post(f"{CLOUD}/v1/server-tokens").respond(200, json={"token": "server-tok"})
    require_cloud(SERVER, CLOUD)
    assert server_token(CLOUD, "secret-at", SERVER) == "server-tok"
    assert len(made) == 2 and all(c.is_closed for c in made)
