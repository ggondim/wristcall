import base64, json, logging, re
import http_ece
import httpx, jwt, pytest, respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import CLIENTS, ISSUER, mongo_url, serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig, ConfigError, config_from_env
from wristcall_cloud.push.channels import ChannelGone, ChannelUnavailable, Message
from wristcall_cloud.push.webpush import DEFAULT_HOSTS, Vapid, WebPushChannel, encrypt, endpoint_allowed
from wristcall_cloud.store import Store


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def browser():
    """What a browser keeps for one subscription: its key pair and auth secret."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return priv, pub, b"0123456789abcdef"


def test_encrypt_matches_reference_decrypter():
    priv, pub, auth = browser()
    body = encrypt(b'{"hello": "world"}', pub, auth)
    assert http_ece.decrypt(body, private_key=priv, auth_secret=auth, version="aes128gcm") == b'{"hello": "world"}'


def test_rfc8291_appendix_a_vector():
    # RFC 8291 Appendix A: fixed keys and salt give this exact body.
    ua_priv = ec.derive_private_key(int.from_bytes(base64.urlsafe_b64decode("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94="), "big"), ec.SECP256R1())
    as_priv = ec.derive_private_key(int.from_bytes(base64.urlsafe_b64decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw="), "big"), ec.SECP256R1())
    ua_pub = ua_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = base64.urlsafe_b64decode("BTBZMqHH6r4Tts7J_aSIgg==")
    salt = base64.urlsafe_b64decode("DGv6ra1nlYgDCS1FRnbzlw==")
    body = encrypt(b"When I grow up, I want to be a watermelon", ua_pub, auth, salt=salt, private_key=as_priv)
    assert b64(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_y"
        "l95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


@pytest.mark.parametrize("endpoint,ok", [
    ("https://web.push.apple.com/QGuQ", True),
    ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
    ("https://push.apple.com.evil.test/x", False),
    ("https://evil.test/x", False),
    ("http://fcm.googleapis.com/fcm/send/abc", False),
    ("https://fcm.googleapis.com:8443/fcm/send/abc", False),
    ("https://u:p@fcm.googleapis.com/x", False),
    ("https://10.0.0.1/x", False),
])
def test_webpush_endpoint_allowlist(endpoint, ok):
    assert endpoint_allowed(endpoint, DEFAULT_HOSTS) is ok


async def test_send_posts_encrypted_payload_with_vapid():
    priv, pub, auth = browser()
    vapid_key = ec.generate_private_key(ec.SECP256R1())
    vapid = Vapid(vapid_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()), "mailto:ops@example.com")
    endpoint = "https://fcm.googleapis.com/fcm/send/abc"
    reg = {"platform": "webpush", "channel": endpoint, "p256dh": b64(pub), "auth": b64(auth), "label": "Home"}
    msg = Message("call.finished", "Delivered", "Ana got it", "Home", {"call_id": "c_1"}, 3600, None, "srv-1")
    with respx.mock() as router:
        route = router.post(endpoint).mock(return_value=httpx.Response(201))
        async with httpx.AsyncClient() as http:
            await WebPushChannel(vapid, http).send(reg, msg)
    req = route.calls.last.request
    assert req.headers["content-encoding"] == "aes128gcm" and req.headers["ttl"] == "3600"
    scheme, _, params = req.headers["authorization"].partition(" ")
    fields = dict(p.strip().split("=", 1) for p in params.split(","))
    assert scheme == "vapid" and fields["k"] == vapid.public_key
    claims = jwt.decode(fields["t"], vapid_key.public_key(), algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:ops@example.com"
    payload = json.loads(http_ece.decrypt(req.content, private_key=priv, auth_secret=auth, version="aes128gcm"))
    assert payload == {"v": 1, "event": "call.finished", "tag": "srv-1", "title": "Delivered", "subtitle": "Home",
                       "body": "Ana got it", "data": {"call_id": "c_1"}}


ENDPOINT = "https://fcm.googleapis.com/fcm/send/s3cr3t-endpoint-id"
VAPID_KEY = ec.generate_private_key(ec.SECP256R1())
VAPID_PEM = VAPID_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
SUBJECT = "mailto:ops@example.com"
MSG = Message("test", "Hello", "", "Home", {}, 60, None, "srv-1")


def subscription(endpoint: str = ENDPOINT) -> tuple[dict, ec.EllipticCurvePrivateKey, bytes]:
    """A stored Web Push registration and what the browser keeps to read its messages."""
    priv, pub, auth = browser()
    reg = {"platform": "webpush", "channel": endpoint, "p256dh": b64(pub), "auth": b64(auth), "label": "Home",
           "tag": "srv-1", "events": ["call.finished"]}
    return reg, priv, auth


def channel(http: httpx.AsyncClient, **kwargs) -> WebPushChannel:
    return WebPushChannel(Vapid(VAPID_PEM, SUBJECT), http, **kwargs)


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), vapid_private_pem=VAPID_PEM,
                       vapid_subject=SUBJECT, registrations_per_minute_per_ip=1000)


@pytest.fixture
def app(config, mongo_db, fake_verifier):
    # No channels given: the app builds the real Web Push channel from its VAPID configuration.
    return create_app(config, store=Store(mongo_db), verifier=fake_verifier)


def register(client, endpoint: str = ENDPOINT) -> tuple[str, ec.EllipticCurvePrivateKey, bytes]:
    priv, pub, auth = browser()
    body = {"platform": "webpush", "label": "Home", "tag": "srv-1", "events": ["call.finished"],
            "subscription": {"endpoint": endpoint, "keys": {"p256dh": b64(pub), "auth": b64(auth)}}}
    r = client.post("/v1/push/registrations", json=body)
    assert r.status_code == 201
    return r.json()["push_key"], priv, auth


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.parametrize("status", [404, 410])
async def test_webpush_gone_removes_registration(status, client):
    reg, _, _ = subscription()
    with respx.mock() as router:
        route = router.post(ENDPOINT).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ChannelGone):
                await channel(http).send(reg, MSG)
        assert route.call_count == 1
        # Through the API: the server is told 410 and the registration is gone.
        key, _, _ = register(client)
        r = client.post("/v1/push/send", json={"event": "test", "title": "t"}, headers=bearer(key))
        assert r.status_code == 410 and r.json()["error"] == "gone"
        assert client.get("/v1/push/registrations/current", headers=bearer(key)).status_code == 410


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_webpush_unavailable(status):
    reg, _, _ = subscription()
    with respx.mock() as router:
        router.post(ENDPOINT).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ChannelUnavailable):
                await channel(http).send(reg, MSG)


@pytest.mark.parametrize("status", [400, 403, 413])
async def test_webpush_refused_is_unavailable_and_logged(status, caplog):
    caplog.set_level(logging.INFO, logger="wristcall_cloud.push")
    reg, _, _ = subscription()
    with respx.mock() as router:
        router.post(ENDPOINT).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ChannelUnavailable):
                await channel(http).send(reg, MSG)
    assert f"status {status}" in caplog.text


async def test_webpush_redirect_is_not_followed():
    reg, _, _ = subscription()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(ENDPOINT).mock(
            return_value=httpx.Response(301, headers={"Location": "http://10.0.0.1/internal"}))
        internal = router.post("http://10.0.0.1/internal").mock(return_value=httpx.Response(201))
        # Even a client that follows redirects does not follow them for a push.
        async with httpx.AsyncClient(follow_redirects=True) as http:
            with pytest.raises(ChannelUnavailable):
                await channel(http).send(reg, MSG)
    assert route.call_count == 1 and internal.call_count == 0


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("e")])
async def test_webpush_timeout_is_unavailable(error):
    reg, _, _ = subscription()
    with respx.mock() as router:
        route = router.post(ENDPOINT).mock(side_effect=error)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ChannelUnavailable):
                await channel(http, timeout_s=4).send(reg, MSG)
    assert route.calls.last.request.extensions["timeout"] == {"connect": 4, "read": 4, "write": 4, "pool": 4}


@pytest.mark.parametrize("endpoint", [
    "https://evil.test/x", "https://10.0.0.1/x", "https://push.apple.com.evil.test/x",
    "http://fcm.googleapis.com/fcm/send/x", "https://fcm.googleapis.com:8443/fcm/send/x",
    "https://u:p@fcm.googleapis.com/x", "https://fcm.googleapis.com\\@evil.test/x",
])
def test_registration_rejects_endpoint_off_the_list(client, endpoint):
    priv, pub, auth = browser()
    body = {"platform": "webpush", "label": "Home", "events": ["call.finished"],
            "subscription": {"endpoint": endpoint, "keys": {"p256dh": b64(pub), "auth": b64(auth)}}}
    with respx.mock(assert_all_called=False) as router:
        r = client.post("/v1/push/registrations", json=body)
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        assert endpoint not in r.text
    assert not router.calls


@pytest.mark.parametrize("endpoint,ok", [
    ("https://push.apple.com/x", False),  # ".push.apple.com" is its subdomains only
    ("https://evilpush.apple.com/x", False),
    ("https://fcm.googleapis.com.evil.test/x", False),
    ("https://sub.fcm.googleapis.com/x", False),  # no leading dot: that host only
    ("https://FCM.GoogleAPIs.com/x", True),
    ("https://fcm.googleapis.com:443/x", True),
    ("https://wns2-par02p.notify.windows.com/w/?token=x", True),
    ("https://fcm.googleapis.com/a\tb", False),
    ("https:///x", False),
    ("not a url", False),
])
def test_endpoint_allowed_edges(endpoint, ok):
    assert endpoint_allowed(endpoint, DEFAULT_HOSTS) is ok


def test_registration_follows_the_configured_list(mongo_db, fake_verifier):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS), vapid_private_pem=VAPID_PEM,
                      vapid_subject=SUBJECT, webpush_hosts=("push.example.com",))
    for c in serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier), mongo_db):
        register(c, "https://push.example.com/x")
        priv, pub, auth = browser()
        body = {"platform": "webpush", "label": "Home", "events": ["call.finished"],
                "subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": b64(pub), "auth": b64(auth)}}}
        assert c.post("/v1/push/registrations", json=body).status_code == 422


def test_vapid_header_is_cached_per_origin():
    vapid = Vapid(VAPID_PEM, SUBJECT)
    now = 1_800_000_000.0
    first = vapid.header("https://fcm.googleapis.com/fcm/send/a", now)
    assert vapid.header("https://fcm.googleapis.com/fcm/send/b", now + 60) == first
    apple = vapid.header("https://web.push.apple.com/x", now)
    assert apple != first

    def claims(header: str) -> dict:
        token = header.removeprefix("vapid t=").partition(",")[0]
        return jwt.decode(token, VAPID_KEY.public_key(), algorithms=["ES256"], options={"verify_aud": False,
                                                                                       "verify_exp": False})

    assert claims(first) == {"aud": "https://fcm.googleapis.com", "exp": int(now) + 12 * 3600, "sub": SUBJECT}
    assert claims(apple)["aud"] == "https://web.push.apple.com"
    # Renewed one hour before it expires.
    assert vapid.header("https://fcm.googleapis.com/x", now + 11 * 3600 - 1) == first
    renewed = vapid.header("https://fcm.googleapis.com/x", now + 11 * 3600)
    assert renewed != first and claims(renewed)["exp"] == int(now) + 23 * 3600


def test_vapid_header_cache_is_bounded():
    vapid = Vapid(VAPID_PEM, SUBJECT)
    now = 1_800_000_000.0
    for i in range(1000):
        vapid.header(f"https://push{i}.example.com/x", now)
    assert len(vapid._cache) == 1000
    # One origin more: the cache starts over instead of growing without end.
    vapid.header("https://push1000.example.com/x", now)
    assert len(vapid._cache) == 1


def test_vapid_refuses_bad_keys_and_subjects():
    p384_pem = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    for pem in (b"not a key", p384_pem):
        with pytest.raises(ValueError):
            Vapid(pem, SUBJECT)
    for subject in ("ops@example.com", "http://example.com", ""):
        with pytest.raises(ValueError):
            Vapid(VAPID_PEM, subject)
    assert Vapid(VAPID_PEM, "https://example.com/contact").public_key == b64(
        VAPID_KEY.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def test_webpush_endpoint_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    key, priv, auth = register(client)
    with respx.mock() as router:
        route = router.post(ENDPOINT)
        for response in (httpx.Response(201), httpx.Response(500), httpx.ConnectTimeout("t"), httpx.Response(410)):
            route.mock(side_effect=[response])
            client.post("/v1/push/send", json={"event": "call.finished", "title": "t"}, headers=bearer(key))
        assert route.call_count == 4
        # The first send went through the real channel: the browser can read it.
        payload = json.loads(http_ece.decrypt(route.calls[0].request.content, private_key=priv, auth_secret=auth,
                                              version="aes128gcm"))
        assert payload["event"] == "call.finished" and payload["subtitle"] == "Home" and payload["tag"] == "srv-1"
    assert client.get("/v1/push/registrations/current", headers=bearer(key)).status_code == 410
    assert caplog.text
    assert ENDPOINT not in caplog.text and "s3cr3t" not in caplog.text and key not in caplog.text


async def test_endpoint_rechecked_at_send():
    reg, _, _ = subscription("https://web.push.apple.com/QGuQ")
    with respx.mock(assert_all_called=False) as router:
        router.post("https://web.push.apple.com/QGuQ").mock(return_value=httpx.Response(201))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ChannelGone):
                await channel(http, hosts=("fcm.googleapis.com",)).send(reg, MSG)
    assert not router.calls


@pytest.mark.parametrize("collapse_id,topic", [
    (None, None), ("call_1-a", "call_1-a"), ("x" * 32, "x" * 32),
    # Not a Topic as is (a dot, a colon or over 32): a stable digest of it, so replacing still works.
    ("call.finished:c_1", "digest"), ("x" * 33, "digest"),
])
async def test_webpush_headers(collapse_id, topic):
    reg, _, _ = subscription()
    msg = Message("test", "t", "", "Home", {}, 0, collapse_id, "")
    with respx.mock() as router:
        route = router.post(ENDPOINT).mock(return_value=httpx.Response(201))
        async with httpx.AsyncClient() as http:
            await channel(http).send(reg, msg)
            await channel(http).send(reg, msg)
    first, second = (call.request.headers for call in route.calls)
    assert first["content-type"] == "application/octet-stream" and first["urgency"] == "high"
    assert first["ttl"] == "0"
    if topic is None:
        assert "topic" not in first
    elif topic == "digest":
        assert re.fullmatch(r"[A-Za-z0-9_-]{32}", first["topic"]) and first["topic"] == second["topic"]
    else:
        assert first["topic"] == topic


def test_encrypt_rejects_what_does_not_fit_one_record():
    _, pub, auth = browser()
    assert len(encrypt(b"x" * 4078, pub, auth)) == 86 + 4078 + 1 + 16
    with pytest.raises(ValueError):
        encrypt(b"x" * 4080, pub, auth)


async def test_invalid_browser_key_is_gone():
    reg, _, _ = subscription()
    reg["p256dh"] = b64(b"\x04" + bytes(range(64)))  # 65 bytes, but not a point of the curve
    with respx.mock(assert_all_called=False) as router:
        async with httpx.AsyncClient() as http:
            with pytest.raises(ChannelGone):
                await channel(http).send(reg, MSG)
    assert not router.calls


def test_config_reports_the_vapid_public_key(client):
    push = client.get("/v1/config").json()["push"]
    assert push["webpush"] is True and push["apns"] is False
    assert push["vapid_public_key"] == Vapid(VAPID_PEM, SUBJECT).public_key


def test_app_closes_its_webpush_client(mongo_db, fake_verifier, config, monkeypatch):
    closed: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.aclose

    async def aclose(self) -> None:
        closed.append(self)
        await original(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", aclose)
    app = create_app(config, store=Store(mongo_db), verifier=fake_verifier)
    webpush = app.state.channels["webpush"]
    assert isinstance(webpush, WebPushChannel) and webpush.http.follow_redirects is False
    for _ in serve(app, mongo_db):
        pass
    assert webpush.http in closed and webpush.http.is_closed


BASE_ENV = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017", "WRISTCALL_CLOUD_CLIENT_IOS": "ios"}


def test_vapid_config_from_env(tmp_path):
    cfg = config_from_env(BASE_ENV)
    assert cfg.vapid_private_pem is None and cfg.vapid_subject is None and cfg.webpush_hosts == DEFAULT_HOSTS
    cfg = config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_VAPID_PRIVATE_KEY": VAPID_PEM.decode(),
                           "WRISTCALL_CLOUD_VAPID_SUBJECT": SUBJECT,
                           "WRISTCALL_CLOUD_WEBPUSH_HOSTS": " Push.Example.com, .push.example.net ,"})
    assert cfg.vapid_private_pem == VAPID_PEM.strip() and cfg.vapid_subject == SUBJECT
    assert cfg.webpush_hosts == ("push.example.com", ".push.example.net")
    assert "BEGIN" not in repr(cfg)
    path = tmp_path / "vapid.pem"
    path.write_bytes(VAPID_PEM)
    cfg = config_from_env({**BASE_ENV, "WRISTCALL_CLOUD_VAPID_PRIVATE_KEY_FILE": str(path),
                           "WRISTCALL_CLOUD_VAPID_SUBJECT": SUBJECT})
    assert cfg.vapid_private_pem == VAPID_PEM


@pytest.mark.parametrize("overrides,name", [
    ({"WRISTCALL_CLOUD_VAPID_PRIVATE_KEY": VAPID_PEM.decode()}, "WRISTCALL_CLOUD_VAPID_SUBJECT"),
    ({"WRISTCALL_CLOUD_VAPID_SUBJECT": SUBJECT}, "WRISTCALL_CLOUD_VAPID_PRIVATE_KEY"),
    ({"WRISTCALL_CLOUD_VAPID_PRIVATE_KEY": "s3cr3t", "WRISTCALL_CLOUD_VAPID_SUBJECT": SUBJECT},
     "WRISTCALL_CLOUD_VAPID_PRIVATE_KEY"),
    ({"WRISTCALL_CLOUD_VAPID_PRIVATE_KEY": VAPID_PEM.decode(), "WRISTCALL_CLOUD_VAPID_SUBJECT": "s3cr3t"},
     "WRISTCALL_CLOUD_VAPID_SUBJECT"),
    ({"WRISTCALL_CLOUD_WEBPUSH_HOSTS": "s3cr3t/x"}, "WRISTCALL_CLOUD_WEBPUSH_HOSTS"),
    ({"WRISTCALL_CLOUD_WEBPUSH_HOSTS": " , "}, "WRISTCALL_CLOUD_WEBPUSH_HOSTS"),
    ({"WRISTCALL_CLOUD_WEBPUSH_HOSTS": "."}, "WRISTCALL_CLOUD_WEBPUSH_HOSTS"),
])
def test_vapid_config_errors(overrides, name):
    with pytest.raises(ConfigError) as e:
        config_from_env({**BASE_ENV, **overrides})
    assert name in str(e.value) and "s3cr3t" not in str(e.value) and "BEGIN" not in str(e.value)


def test_create_app_checks_the_vapid_config(mongo_db, fake_verifier):
    base = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients=dict(CLIENTS))
    for cfg in (CloudConfig(**{**base.__dict__, "vapid_private_pem": VAPID_PEM}),
                CloudConfig(**{**base.__dict__, "vapid_private_pem": b"x", "vapid_subject": SUBJECT})):
        with pytest.raises(ConfigError):
            create_app(cfg, store=Store(mongo_db), verifier=fake_verifier)
