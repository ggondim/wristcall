import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hpack, httpx, jwt, pytest, respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import CLIENTS, ISSUER, mongo_url, serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig, ConfigError, config_from_env
from wristcall_cloud.push.apns import APNS_HOSTS, CATEGORIES, ApnsChannel, ProviderToken
from wristcall_cloud.push.channels import ChannelGone, ChannelUnavailable, Message
from wristcall_cloud.store import Store

TOPIC = "io.github.ggondim.wristcall"
DEVICE = "ab" * 32
HOST = "https://apns.test"
URL = f"{HOST}/3/device/{DEVICE}"
KEY = ec.generate_private_key(ec.SECP256R1())
KEY_PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
REG = {"platform": "apns", "channel": DEVICE, "topic": TOPIC, "environment": "sandbox", "label": "Home",
       "tag": "srv-1", "events": ["call.finished"]}
MSG = Message("call.finished", "Delivered", "Ana got it", "Home", {"call_id": "c_1"}, 3600, "c_1", "srv-1")


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def apns():
    async with httpx.AsyncClient() as http:
        yield ApnsChannel(ProviderToken(KEY_PEM, "KEY123", "TEAM456"), http, hosts={"sandbox": HOST}), KEY


def refused(status: int, why: str) -> httpx.Response:
    return httpx.Response(status, json={"reason": why})


async def test_apns_request_shape(apns):
    channel, key = apns
    reg = {"platform": "apns", "channel": "ab" * 32, "topic": "io.github.ggondim.wristcall", "environment": "sandbox", "label": "Home"}
    msg = Message("call.finished", "Delivered", "Ana got it", "Home", {"call_id": "c_1"}, 3600, "c_1", "srv-1")
    with respx.mock() as router:
        route = router.post(f"https://apns.test/3/device/{'ab' * 32}").mock(return_value=httpx.Response(200))
        await channel.send(reg, msg)
    req = route.calls.last.request
    assert req.headers["apns-topic"] == "io.github.ggondim.wristcall" and req.headers["apns-push-type"] == "alert"
    assert req.headers["apns-priority"] == "10" and req.headers["apns-collapse-id"] == "c_1"
    scheme, _, token = req.headers["authorization"].partition(" ")
    assert scheme == "bearer"
    assert jwt.get_unverified_header(token)["kid"] == "KEY123"
    assert jwt.decode(token, key.public_key(), algorithms=["ES256"])["iss"] == "TEAM456"
    body = json.loads(req.content)
    assert body["aps"]["alert"] == {"title": "Delivered", "subtitle": "Home", "body": "Ana got it"}
    assert body["aps"]["category"] == "WC_CALL_FINISHED"
    assert body["wristcall"] == {"v": 1, "event": "call.finished", "tag": "srv-1", "data": {"call_id": "c_1"}}


async def test_apns_headers_details():
    clock = Clock()
    async with httpx.AsyncClient() as http:
        channel = ApnsChannel(ProviderToken(KEY_PEM, "KEY123", "TEAM456", now=clock), http, hosts={"sandbox": HOST},
                              now=clock)
        with respx.mock() as router:
            route = router.post(URL).mock(return_value=httpx.Response(200))
            await channel.send(REG, MSG)
            await channel.send(REG, Message("test", "t", "", "Home", {}, 0, None, ""))
    first, second = (call.request for call in route.calls)
    assert first.headers["apns-expiration"] == str(int(clock.now) + 3600)
    assert first.headers["content-type"] == "application/json"
    header = jwt.get_unverified_header(first.headers["authorization"].partition(" ")[2])
    assert header == {"alg": "ES256", "kid": "KEY123"}
    claims = jwt.decode(first.headers["authorization"].partition(" ")[2], KEY.public_key(), algorithms=["ES256"])
    assert claims == {"iss": "TEAM456", "iat": int(clock.now)}
    aps = json.loads(first.content)["aps"]
    assert aps["sound"] == "default" and aps["thread-id"] == "call.finished"
    assert second.headers["apns-expiration"] == "0" and "apns-collapse-id" not in second.headers
    body = json.loads(second.content)
    assert body["aps"]["category"] == "WC_TEST" and body["wristcall"]["tag"] == ""
    assert CATEGORIES == {"call.finished": "WC_CALL_FINISHED", "device.approval": "WC_DEVICE_APPROVAL",
                          "test": "WC_TEST"}


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,),
                       apns_key_pem=KEY_PEM, apns_key_id="KEY123", apns_team_id="TEAM456",
                       apns_hosts={"sandbox": HOST, "production": "https://apns-production.test"},
                       registrations_per_minute_per_ip=1000)


@pytest.fixture
def app(config, mongo_db, fake_verifier):
    # No channels given: the app builds the real APNs channel from its configuration.
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier)


def register(client, environment: str = "sandbox") -> str:
    body = {"platform": "apns", "token": DEVICE, "topic": TOPIC, "environment": environment, "label": "Home",
            "tag": "srv-1", "events": ["call.finished"]}
    r = client.post("/v1/push/registrations", json=body)
    assert r.status_code == 201
    return r.json()["push_key"]


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.parametrize("status,reason", [(410, "Unregistered"), (400, "BadDeviceToken"), (400, "DeviceTokenNotForTopic")])
async def test_apns_unregistered_removes_registration(apns, status, reason, client):
    channel, _ = apns
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=refused(status, reason))
        with pytest.raises(ChannelGone):
            await channel.send(REG, MSG)
        assert route.call_count == 1
        # Through the API: the server is told 410 and the registration is gone.
        key = register(client)
        r = client.post("/v1/push/send", json={"event": "call.finished", "title": "t"}, headers=bearer(key))
        assert r.status_code == 410 and r.json()["error"] == "gone"
        assert client.get("/v1/push/registrations/current", headers=bearer(key)).status_code == 410
        assert route.call_count == 2


@pytest.mark.parametrize("reason", ["BadDeviceToken", "DeviceTokenNotForTopic"])
async def test_apns_wrong_environment_or_topic_is_warned_about(apns, reason, caplog):
    # A whole fleet of these at once is a configuration mistake (sandbox token sent to production, wrong bundle id),
    # not devices gone: the operator is told, with the reason only.
    channel, _ = apns
    with respx.mock() as router, caplog.at_level(logging.INFO, logger="wristcall_cloud.push"):
        router.post(URL).mock(return_value=refused(400, reason))
        with pytest.raises(ChannelGone):
            await channel.send(REG, MSG)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert reason in text and "environment" in text and "topic" in text
    assert DEVICE not in caplog.text and TOPIC not in caplog.text


async def test_apns_unregistered_is_not_warned_about(apns, caplog):
    channel, _ = apns
    with respx.mock() as router, caplog.at_level(logging.INFO, logger="wristcall_cloud.push"):
        router.post(URL).mock(return_value=refused(410, "Unregistered"))
        with pytest.raises(ChannelGone):
            await channel.send(REG, MSG)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("why", ["ExpiredProviderToken", "InvalidProviderToken"])
async def test_expired_provider_token_is_refreshed_once(why):
    clock = Clock()
    async with httpx.AsyncClient() as http:
        channel = ApnsChannel(ProviderToken(KEY_PEM, "KEY123", "TEAM456", now=clock), http, hosts={"sandbox": HOST})
        with respx.mock() as router:
            route = router.post(URL).mock(side_effect=[refused(403, why), httpx.Response(200)])
            await channel.send(REG, MSG)
            assert route.call_count == 2
            first, second = (c.request.headers["authorization"] for c in route.calls)
            assert first != second
            # A second refusal is not retried again: one new try, then unavailable.
            route.mock(side_effect=[refused(403, why), refused(403, why), httpx.Response(200)])
            with pytest.raises(ChannelUnavailable):
                await channel.send(REG, MSG)
            assert route.call_count == 4


@pytest.mark.parametrize("why", ["ExpiredProviderToken", "InvalidProviderToken"])
async def test_provider_token_dropped_only_on_the_first_refusal(why):
    clock = Clock()
    token = ProviderToken(KEY_PEM, "KEY123", "TEAM456", now=clock)
    async with httpx.AsyncClient() as http:
        channel = ApnsChannel(token, http, hosts={"sandbox": HOST})
        with respx.mock() as router:
            route = router.post(URL).mock(side_effect=[refused(403, why), refused(403, why)])
            with pytest.raises(ChannelUnavailable):
                await channel.send(REG, MSG)
            first, second = (c.request.headers["authorization"].partition(" ")[2] for c in route.calls)
            assert first != second
            # The renewed token was refused on the retry, but it is not discarded: sends that follow reuse it
            # instead of asking for yet another one (APNs: TooManyProviderTokenUpdates).
            assert token.get() == second


async def test_apns_throttled_is_unavailable(apns):
    channel, _ = apns
    with respx.mock() as router:
        router.post(URL).mock(return_value=refused(429, "TooManyRequests"))
        with pytest.raises(ChannelUnavailable):
            await channel.send(REG, MSG)


@pytest.mark.parametrize("response", [
    refused(500, "InternalServerError"), refused(503, "ServiceUnavailable"), refused(400, "BadCollapseId"),
    refused(403, "BadCertificate"), refused(413, "PayloadTooLarge"), httpx.Response(400, text="not json"),
    httpx.ConnectError("e"), httpx.ReadTimeout("t"),
])
async def test_apns_other_failures_are_unavailable(apns, response):
    channel, _ = apns
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=[response])
        with pytest.raises(ChannelUnavailable):
            await channel.send(REG, MSG)
    assert route.call_count == 1


async def test_apns_timeout_is_applied():
    async with httpx.AsyncClient() as http:
        channel = ApnsChannel(ProviderToken(KEY_PEM, "KEY123", "TEAM456"), http, hosts={"sandbox": HOST}, timeout_s=4)
        with respx.mock() as router:
            route = router.post(URL).mock(side_effect=httpx.ConnectTimeout("t"))
            with pytest.raises(ChannelUnavailable):
                await channel.send(REG, MSG)
    assert route.calls.last.request.extensions["timeout"] == {"connect": 4, "read": 4, "write": 4, "pool": 4}


async def test_provider_token_is_reused():
    clock = Clock()
    token = ProviderToken(KEY_PEM, "KEY123", "TEAM456", now=clock)
    first = token.get()
    clock.now += 10 * 60
    assert token.get() == first
    clock.now += 21 * 60
    second = token.get()
    assert second != first
    assert jwt.decode(second, KEY.public_key(), algorithms=["ES256"])["iat"] == int(clock.now)


def test_provider_token_invalidate_only_the_current_one():
    clock = Clock()
    token = ProviderToken(KEY_PEM, "KEY123", "TEAM456", now=clock)
    first = token.get()
    clock.now += 1
    token.invalidate(first)
    second = token.get()
    assert second != first
    # A send that started with the first token and failed later must not throw away the second one.
    token.invalidate(first)
    assert token.get() == second


def test_provider_token_refuses_bad_keys():
    p384 = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    for pem in (b"not a key", p384):
        with pytest.raises(ValueError):
            ProviderToken(pem, "KEY123", "TEAM456")


async def test_environment_picks_the_host():
    async with httpx.AsyncClient() as http:
        channel = ApnsChannel(ProviderToken(KEY_PEM, "KEY123", "TEAM456"), http)
        with respx.mock() as router:
            production = router.post(f"{APNS_HOSTS['production']}/3/device/{DEVICE}").mock(
                return_value=httpx.Response(200))
            sandbox = router.post(f"{APNS_HOSTS['sandbox']}/3/device/{DEVICE}").mock(return_value=httpx.Response(200))
            await channel.send({**REG, "environment": "production"}, MSG)
            assert production.call_count == 1 and sandbox.call_count == 0
            await channel.send(REG, MSG)
            assert sandbox.call_count == 1
    assert APNS_HOSTS == {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}


async def test_unknown_environment_is_unavailable(apns):
    channel, _ = apns
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(ChannelUnavailable):
            await channel.send({**REG, "environment": "production"}, MSG)  # the fixture only knows the sandbox
    assert not router.calls


async def test_payload_over_4096_is_refused(apns):
    channel, _ = apns
    big = Message("call.finished", "t" * 100, "b" * 300, "Home", {"x": "é" * 2000}, 3600, None, "srv-1")
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(ChannelUnavailable):
            await channel.send(REG, big)
    assert not router.calls


async def test_device_token_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    key = register(client)
    sends = ([httpx.Response(200)], [refused(500, "InternalServerError")], [httpx.ConnectTimeout("t")],
             [refused(403, "ExpiredProviderToken"), httpx.Response(200)], [refused(410, "Unregistered")])
    with respx.mock() as router:
        route = router.post(URL)
        for responses in sends:
            route.mock(side_effect=responses)
            client.post("/v1/push/send", json={"event": "call.finished", "title": "t"}, headers=bearer(key))
        assert route.call_count == 6
        assert json.loads(route.calls[0].request.content)["aps"]["alert"]["subtitle"] == "Home"
    assert client.get("/v1/push/registrations/current", headers=bearer(key)).status_code == 410
    # HTTP/2 header compression logs every header at DEBUG, the request path (the device token) included.
    hpack.Encoder().encode([(":path", f"/3/device/{DEVICE}"), ("authorization", "bearer x.y.z")])
    assert caplog.text and "status 410" in caplog.text
    assert DEVICE not in caplog.text and DEVICE.upper() not in caplog.text and key not in caplog.text
    assert "bearer" not in caplog.text.lower()


def test_config_reports_apns(client):
    push = client.get("/v1/config").json()["push"]
    assert push["apns"] is True and push["webpush"] is False and push["apns_topics"] == [TOPIC]


def test_app_closes_its_apns_client(mongo_db, fake_verifier, config, monkeypatch):
    closed: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.aclose

    async def aclose(self) -> None:
        closed.append(self)
        await original(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", aclose)
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier)
    apns = app.state.channels["apns"]
    assert isinstance(apns, ApnsChannel) and apns.hosts["sandbox"] == HOST
    assert apns.http.follow_redirects is False
    for _ in serve(app, mongo_db):
        pass
    assert apns.http in closed and apns.http.is_closed


def test_no_apns_without_configuration(mongo_db, fake_verifier):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,))
    app = create_app(cfg, store=Store(mongo_db), verifier=fake_verifier)
    assert "apns" not in app.state.channels
    for c in serve(app, mongo_db):
        push = c.get("/v1/config").json()["push"]
        # No channel, no topics: an app must not offer a registration this Cloud would answer with 404.
        assert push["apns"] is False and push["apns_topics"] == []
        r = c.post("/v1/push/registrations", json={"platform": "apns", "token": DEVICE, "topic": TOPIC,
                                                    "environment": "sandbox", "label": "Home",
                                                    "events": ["call.finished"]})
        assert r.status_code == 404 and r.json()["error"] == "not_configured"


class FakeApns(BaseHTTPRequestHandler):
    received: list[tuple[str, dict[str, str], bytes]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeApns.received.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(200)
        self.send_header("apns-id", "00000000-0000-0000-0000-000000000000")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # quiet
        pass


async def test_apns_against_local_fake_server():
    FakeApns.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApns)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        async with httpx.AsyncClient(http2=True) as http:
            channel = ApnsChannel(ProviderToken(KEY_PEM, "KEY123", "TEAM456"), http,
                                  hosts={"sandbox": f"http://127.0.0.1:{port}"})
            await channel.send(REG, MSG)
    finally:
        server.shutdown()
        server.server_close()
    [(path, headers, body)] = FakeApns.received
    assert path == f"/3/device/{DEVICE}"
    assert headers["apns-topic"] == TOPIC and headers["apns-push-type"] == "alert" and headers["apns-priority"] == "10"
    assert headers["apns-collapse-id"] == "c_1"
    scheme, _, token = headers["authorization"].partition(" ")
    assert scheme == "bearer" and jwt.decode(token, KEY.public_key(), algorithms=["ES256"])["iss"] == "TEAM456"
    assert json.loads(body)["wristcall"] == {"v": 1, "event": "call.finished", "tag": "srv-1",
                                             "data": {"call_id": "c_1"}}


BASE_ENV = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017", "WRISTCALL_CLOUD_CLIENT_IOS": "ios"}
APNS_ENV = {"WRISTCALL_CLOUD_APNS_KEY": KEY_PEM.decode(), "WRISTCALL_CLOUD_APNS_KEY_ID": "KEY123",
            "WRISTCALL_CLOUD_APNS_TEAM_ID": "TEAM456", "WRISTCALL_CLOUD_APNS_TOPICS": TOPIC}


def test_apns_config_from_env(tmp_path):
    cfg = config_from_env(BASE_ENV)
    assert cfg.apns_key_pem is None and cfg.apns_key_id is None and cfg.apns_team_id is None
    assert cfg.apns_hosts == APNS_HOSTS
    cfg = config_from_env({**BASE_ENV, **APNS_ENV})
    assert cfg.apns_key_pem == KEY_PEM.strip() and cfg.apns_key_id == "KEY123" and cfg.apns_team_id == "TEAM456"
    assert "BEGIN" not in repr(cfg)
    path = tmp_path / "AuthKey_KEY123.p8"
    path.write_bytes(KEY_PEM)
    env = {k: v for k, v in APNS_ENV.items() if k != "WRISTCALL_CLOUD_APNS_KEY"}
    cfg = config_from_env({**BASE_ENV, **env, "WRISTCALL_CLOUD_APNS_KEY_FILE": str(path),
                           "WRISTCALL_CLOUD_APNS_URL_SANDBOX": "http://127.0.0.1:8443/",
                           "WRISTCALL_CLOUD_APNS_URL_PRODUCTION": "https://apns.example.com"})
    assert cfg.apns_key_pem == KEY_PEM
    assert cfg.apns_hosts == {"production": "https://apns.example.com", "sandbox": "http://127.0.0.1:8443"}


def test_apns_config_goes_together():
    without_team = {k: v for k, v in APNS_ENV.items() if k != "WRISTCALL_CLOUD_APNS_TEAM_ID"}
    with pytest.raises(ConfigError):
        config_from_env({**BASE_ENV, **without_team})
    without_topics = {k: v for k, v in APNS_ENV.items() if k != "WRISTCALL_CLOUD_APNS_TOPICS"}
    with pytest.raises(ConfigError):
        config_from_env({**BASE_ENV, **without_topics})


@pytest.mark.parametrize("overrides,name", [
    ({"WRISTCALL_CLOUD_APNS_KEY": "s3cr3t"}, "WRISTCALL_CLOUD_APNS_KEY"),
    ({"WRISTCALL_CLOUD_APNS_KEY_ID": None}, "WRISTCALL_CLOUD_APNS_KEY_ID"),
    ({"WRISTCALL_CLOUD_APNS_KEY": None}, "WRISTCALL_CLOUD_APNS_KEY"),
    ({"WRISTCALL_CLOUD_APNS_TOPICS": None}, "WRISTCALL_CLOUD_APNS_TOPICS"),
    ({"WRISTCALL_CLOUD_APNS_KEY_ID": "s3cr3t/x"}, "WRISTCALL_CLOUD_APNS_KEY_ID"),
    ({"WRISTCALL_CLOUD_APNS_URL_SANDBOX": "http://s3cr3t.example.com"}, "WRISTCALL_CLOUD_APNS_URL_SANDBOX"),
    ({"WRISTCALL_CLOUD_APNS_URL_PRODUCTION": "ftp://s3cr3t"}, "WRISTCALL_CLOUD_APNS_URL_PRODUCTION"),
])
def test_apns_config_errors(overrides, name):
    env = {**BASE_ENV, **APNS_ENV, **overrides}
    env = {k: v for k, v in env.items() if v is not None}
    with pytest.raises(ConfigError) as e:
        config_from_env(env)
    assert name in str(e.value) and "s3cr3t" not in str(e.value) and "BEGIN" not in str(e.value)


def test_create_app_checks_the_apns_config(mongo_db, fake_verifier):
    base = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), apns_topics=(TOPIC,))
    for changes in ({"apns_key_pem": KEY_PEM}, {"apns_key_pem": b"x", "apns_key_id": "K", "apns_team_id": "T"},
                    {"apns_key_pem": KEY_PEM, "apns_key_id": "K", "apns_team_id": "T", "apns_topics": ()}):
        with pytest.raises(ConfigError):
            create_app(CloudConfig(**{**base.__dict__, **changes}), store=Store(mongo_db), verifier=fake_verifier)
