import httpx
import pytest
import respx

from refclient import __version__
from refclient.client import PairError, pair_account
from refclient.device_flow import DeviceCode, DeviceFlowError, discover, start, wait_for_token

ISSUER = "https://auth.example.com"
DEVICE_EP = f"{ISSUER}/oauth/v2/device_authorization"
TOKEN_EP = f"{ISSUER}/oauth/v2/token"
DISCOVERY = {"issuer": ISSUER, "device_authorization_endpoint": DEVICE_EP, "token_endpoint": TOKEN_EP}
SERVER = "https://wc.example.com"


def code(expires_in: int = 300, interval: int = 5) -> DeviceCode:
    return DeviceCode("dev-code", "ABCD-EFGH", f"{ISSUER}/device", f"{ISSUER}/device?user_code=ABCD-EFGH", expires_in, interval)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self) -> float:
        return self.t


@respx.mock
def test_device_flow_happy_path():
    respx.get(f"{ISSUER}/.well-known/openid-configuration").respond(200, json=DISCOVERY)
    respx.post(DEVICE_EP).respond(
        200,
        json={
            "device_code": "dev-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": f"{ISSUER}/device",
            "verification_uri_complete": f"{ISSUER}/device?user_code=ABCD-EFGH",
            "expires_in": 300,
            "interval": 5,
        },
    )
    route = respx.post(TOKEN_EP)
    route.side_effect = [
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
    ]
    clock = Clock()
    with httpx.Client() as http:
        device, token_endpoint = start(ISSUER, "cid", "openid", http)
        assert token_endpoint == TOKEN_EP
        assert device.user_code == "ABCD-EFGH"
        assert device.verification_uri_complete.endswith("user_code=ABCD-EFGH")
        tokens = wait_for_token(token_endpoint, "cid", device, http, sleep=clock.sleep, now=clock.now)
    assert tokens["access_token"] == "at"
    assert clock.sleeps == [5, 10]
    body = route.calls.last.request.content.decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in body
    assert "device_code=dev-code" in body and "client_id=cid" in body


@respx.mock
def test_device_flow_denied():
    respx.post(TOKEN_EP).respond(400, json={"error": "access_denied"})
    with httpx.Client() as http, pytest.raises(DeviceFlowError, match="denied"):
        wait_for_token(TOKEN_EP, "cid", code(), http, sleep=Clock().sleep, now=Clock().now)


@respx.mock
def test_device_flow_server_expired_token():
    respx.post(TOKEN_EP).respond(400, json={"error": "expired_token"})
    with httpx.Client() as http, pytest.raises(DeviceFlowError, match="expired"):
        wait_for_token(TOKEN_EP, "cid", code(), http, sleep=Clock().sleep, now=Clock().now)


@respx.mock
def test_device_flow_unknown_error():
    respx.post(TOKEN_EP).respond(400, json={"error": "invalid_client"})
    with httpx.Client() as http, pytest.raises(DeviceFlowError, match="invalid_client"):
        wait_for_token(TOKEN_EP, "cid", code(), http, sleep=Clock().sleep, now=Clock().now)


@respx.mock
def test_device_flow_expires():
    respx.post(TOKEN_EP).respond(400, json={"error": "authorization_pending"})
    clock = Clock()
    with httpx.Client() as http, pytest.raises(DeviceFlowError, match="expired"):
        wait_for_token(TOKEN_EP, "cid", code(expires_in=12), http, sleep=clock.sleep, now=clock.now)
    assert clock.t <= 12 + 5


@respx.mock
@pytest.mark.parametrize("missing", ["device_authorization_endpoint", "token_endpoint"])
def test_device_flow_requires_endpoints(missing):
    doc = {k: v for k, v in DISCOVERY.items() if k != missing}
    respx.get(f"{ISSUER}/.well-known/openid-configuration").respond(200, json=doc)
    with httpx.Client() as http, pytest.raises(DeviceFlowError, match=missing):
        discover(ISSUER, http)


@respx.mock
def test_requests_send_user_agent():
    disc = respx.get(f"{ISSUER}/.well-known/openid-configuration").respond(200, json=DISCOVERY)
    dev = respx.post(DEVICE_EP).respond(
        200, json={"device_code": "d", "user_code": "u", "verification_uri": "v", "expires_in": 60, "interval": 1}
    )
    tok = respx.post(TOKEN_EP).respond(200, json={"access_token": "at"})
    with httpx.Client() as http:
        device, endpoint = start(ISSUER, "cid", "openid", http)
        assert device.verification_uri_complete is None
        wait_for_token(endpoint, "cid", device, http, sleep=Clock().sleep, now=Clock().now)
    for route in (disc, dev, tok):
        assert route.calls.last.request.headers["user-agent"] == f"wristcall-refclient/{__version__}"


@respx.mock
def test_pair_account_attestation():
    route = respx.post(f"{SERVER}/v1/pair/account").respond(200, json={"device_id": "d1", "token": "wct"})
    with httpx.Client() as http:
        creds = pair_account(SERVER + "/", "acct-token", "Mac", http=http)
    assert creds == {"device_id": "d1", "token": "wct"}
    import json

    assert json.loads(route.calls.last.request.content) == {"token": "acct-token", "device_name": "Mac"}


@respx.mock
def test_pair_account_approval_then_token(capsys):
    respx.post(f"{SERVER}/v1/pair/account").respond(
        202, json={"request_id": "req1", "poll_token": "ptok", "expires_at": "2030-01-01T00:00:00Z"}
    )
    poll = respx.post(f"{SERVER}/v1/pair/poll")
    poll.side_effect = [httpx.Response(202, json={"status": "pending"}), httpx.Response(200, json={"device_id": "d1", "token": "wct"})]
    with httpx.Client() as http:
        creds = pair_account(SERVER, "acct-token", "Mac", poll_interval_s=0, http=http)
    assert creds["token"] == "wct"
    out = capsys.readouterr().out
    assert "Waiting for approval of request req1" in out
    assert "wristcall devices approve req1" in out
    assert "acct-token" not in out and "ptok" not in out


@respx.mock
def test_pair_account_not_linked_raises():
    respx.post(f"{SERVER}/v1/pair/account").respond(403, json={"error": "not_linked", "message": "no user is linked"})
    with httpx.Client() as http, pytest.raises(PairError, match="403.*not_linked"):
        pair_account(SERVER, "acct-token", "Mac", http=http)


@respx.mock
def test_pair_account_poll_closed_raises():
    respx.post(f"{SERVER}/v1/pair/account").respond(202, json={"request_id": "r", "poll_token": "p", "expires_at": "x"})
    respx.post(f"{SERVER}/v1/pair/poll").respond(410, json={"error": "gone", "message": "request closed"})
    with httpx.Client() as http, pytest.raises(PairError, match="410"):
        pair_account(SERVER, "acct-token", "Mac", poll_interval_s=0, http=http)


@respx.mock
def test_cli_login_then_pair_account(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    import stat

    from refclient import __main__ as cli

    monkeypatch.setattr(cli, "CONFIG", tmp_path / "refclient.json")
    monkeypatch.setattr(cli, "ACCOUNT", tmp_path / "account.json")
    respx.get(f"{ISSUER}/.well-known/openid-configuration").respond(200, json=DISCOVERY)
    respx.post(DEVICE_EP).respond(
        200, json={"device_code": "d", "user_code": "ABCD", "verification_uri": f"{ISSUER}/device", "expires_in": 60, "interval": 1}
    )
    respx.post(TOKEN_EP).respond(200, json={"access_token": "secret-at", "refresh_token": "secret-rt", "expires_in": 3600})
    cloud = "https://cloud.example.com"
    respx.get(f"{SERVER}/v1/health").respond(200, json={"account": {"issuer": cloud}})
    respx.post(f"{cloud}/v1/server-tokens").respond(200, json={"token": "secret-st"})
    respx.post(f"{SERVER}/v1/pair/account").respond(200, json={"device_id": "d1", "token": "secret-wct"})

    assert cli.cmd_login(argparse.Namespace(issuer=ISSUER, client_id="cid", scope="openid")) == 0
    saved = json.loads((tmp_path / "account.json").read_text())
    assert saved["issuer"] == ISSUER and saved["access_token"] == "secret-at" and saved["refresh_token"] == "secret-rt"
    assert saved["expires_at"] > 0
    assert stat.S_IMODE((tmp_path / "account.json").stat().st_mode) == 0o600

    assert cli.cmd_pair_account(argparse.Namespace(server=SERVER, cloud=cloud, name="Mac")) == 0
    assert json.loads((tmp_path / "refclient.json").read_text()) == {"server": SERVER, "token": "secret-wct"}
    assert stat.S_IMODE((tmp_path / "refclient.json").stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert "ABCD" in out.out
    for secret in ("secret-at", "secret-rt", "secret-st", "secret-wct"):
        assert secret not in out.out + out.err


def test_cli_pair_account_requires_login(tmp_path, monkeypatch, capsys):
    import argparse

    from refclient import __main__ as cli

    monkeypatch.setattr(cli, "ACCOUNT", tmp_path / "missing.json")
    assert cli.cmd_pair_account(argparse.Namespace(server=SERVER, cloud="https://cloud.example.com", name="Mac")) == 1
    assert "login" in capsys.readouterr().err
