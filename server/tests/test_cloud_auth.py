"""CloudAuthenticator: the per-server token (`wc-server+jwt`) becomes the request principal in cloud mode."""

import logging
import time

import pytest
from fake_cloud import CLOUD_URL, FakeCloud

from wristcall.auth import Principal
from wristcall.cloud_node.auth import PLATFORMS, CloudAuthenticator
from wristcall.cloud_node.proof import UserProof, current_proof
from wristcall.oidc import OidcVerifier

NODE_URL = "https://node.test"
SECRET = "wc_node_" + "s" * 43


@pytest.fixture
def fake() -> FakeCloud:
    return FakeCloud(NODE_URL, SECRET)


@pytest.fixture
def auth(fake: FakeCloud) -> CloudAuthenticator:
    return CloudAuthenticator(OidcVerifier(CLOUD_URL, [NODE_URL], fake.http(), typ="wc-server+jwt"))


@pytest.fixture(autouse=True)
def clean_proof():
    token = current_proof.set(None)
    yield
    current_proof.reset(token)


def bearer(token: str) -> str:
    return f"Bearer {token}"


def test_platforms() -> None:
    assert PLATFORMS == ("ios", "ipados", "macos", "watchos", "web")


async def test_watch_token_is_device_principal(fake: FakeCloud, auth: CloudAuthenticator) -> None:
    token = fake.user_token("acc-1", "dev-1", "watchos")
    principal = await auth.authenticate(bearer(token))
    assert isinstance(principal, Principal)
    assert principal.user_id == "acc-1"
    assert principal.kind == "device"
    assert principal.token_id is None
    assert principal.platform == "watchos"
    assert principal.device is not None
    assert (principal.device.id, principal.device.user_id, principal.device.name) == ("dev-1", "acc-1", "watchos")
    assert principal.device.created_at == 0.0
    assert principal.device.revoked_at is None
    assert current_proof.get() == UserProof("acc-1", "dev-1", "watchos", token)
    assert current_proof.get().token == token


@pytest.mark.parametrize("platform", ["ios", "ipados", "macos", "web"])
async def test_app_token_is_api_principal(fake: FakeCloud, auth: CloudAuthenticator, platform: str) -> None:
    token = fake.user_token("acc-2", "dev-2", platform)
    principal = await auth.authenticate(bearer(token))
    assert principal is not None
    assert principal.user_id == "acc-2"
    assert principal.kind == "api"
    assert principal.platform == platform
    assert principal.token_id is None
    assert principal.device is not None
    assert (principal.device.id, principal.device.user_id, principal.device.name) == ("dev-2", "acc-2", platform)
    assert current_proof.get() == UserProof("acc-2", "dev-2", platform, token)


async def test_identify_returns_principal_and_proof(fake: FakeCloud, auth: CloudAuthenticator) -> None:
    token = fake.user_token("acc-1", "dev-1", "web")
    found = await auth.identify(bearer(token))
    assert found is not None
    principal, proof = found
    assert principal.user_id == "acc-1"
    assert proof == UserProof("acc-1", "dev-1", "web", token)


@pytest.mark.parametrize(
    "case",
    ["no_did", "no_plt", "no_jti", "bad_platform", "other_node", "wrong_typ", "expired", "no_header", "pat", "not_bearer",
     "garbage", "id_token"],
)
async def test_refuses(fake: FakeCloud, auth: CloudAuthenticator, case: str) -> None:
    header: str | None
    if case == "no_did":
        header = bearer(fake.user_token("acc-1", None, "ios"))
    elif case == "no_plt":
        header = bearer(fake.user_token("acc-1", "dev-1", None))
    elif case == "no_jti":
        header = bearer(fake.user_token("acc-1", "dev-1", "ios", jti=None))
    elif case == "bad_platform":
        header = bearer(fake.user_token("acc-1", "dev-1", "android"))
    elif case == "other_node":
        header = bearer(fake.user_token("acc-1", "dev-1", "ios", aud="https://other.test"))
    elif case == "wrong_typ":
        header = bearer(fake.user_token("acc-1", "dev-1", "ios", typ="JWT"))
    elif case == "expired":
        past = int(time.time()) - 3600
        header = bearer(fake.user_token("acc-1", "dev-1", "ios", iat=past - 300, exp=past))
    elif case == "no_header":
        header = None
    elif case == "pat":
        header = bearer("wc_pat_" + "a" * 43)
    elif case == "not_bearer":
        header = "Basic " + fake.user_token("acc-1", "dev-1", "ios")
    elif case == "garbage":
        header = bearer("not.a.jwt")
    else:
        header = bearer(fake.user_token("acc-1", "dev-1", "ios", nonce="n"))
    assert await auth.authenticate(header) is None
    assert await auth.identify(header) is None
    assert current_proof.get() is None


async def test_jwks_down_is_none(fake: FakeCloud, auth: CloudAuthenticator) -> None:
    token = fake.user_token("acc-1", "dev-1", "ios")
    fake.down = True
    assert await auth.authenticate(bearer(token)) is None
    assert current_proof.get() is None


async def test_never_logs_the_token(fake: FakeCloud, auth: CloudAuthenticator, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    good = fake.user_token("acc-1", "dev-1", "ios")
    bad = fake.user_token("acc-1", None, "ios")
    await auth.authenticate(bearer(good))
    await auth.authenticate(bearer(bad))
    assert good not in caplog.text and bad not in caplog.text


@pytest.mark.parametrize("header", [None, "Bearer not.a.jwt", "Bearer " + "wc_pat_" + "a" * 43])
async def test_refusal_clears_a_stale_proof(auth: CloudAuthenticator, header: str | None) -> None:
    current_proof.set(UserProof("other-account", "dev-x", "ios", "earlier-token"))
    assert await auth.authenticate(header) is None
    assert current_proof.get() is None
