import base64
import re
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from wristcall_cloud.session_tokens import (
    ACCOUNT_TOKEN_TYPE,
    APP_PLATFORMS,
    MANAGER_PLATFORMS,
    PLATFORMS,
    AccountClaims,
    AccountTokens,
    TokenError,
    new_account_id,
    new_code,
    new_device_id,
    new_secret,
    parse_account_key,
    secret_hash,
)
from wristcall_cloud.signing import SigningKey, server_token_claims

ISSUER = "https://cloud.test"
NOW = 1_800_000_000.0


def new_key() -> SigningKey:
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return SigningKey(pem)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def key() -> SigningKey:
    return new_key()


@pytest.fixture
def tokens(key) -> AccountTokens:
    return AccountTokens(key, ISSUER, now=lambda: NOW)


def claims(**overrides):
    base = {
        "iss": ISSUER,
        "aud": ISSUER,
        "sub": "acc_x",
        "did": "dev_x",
        "plt": "ios",
        "iat": int(NOW),
        "exp": int(NOW) + 3600,
        "jti": "abc",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_platform_groups():
    assert PLATFORMS == ("ios", "ipados", "macos", "watchos", "web")
    assert APP_PLATFORMS == {"ios", "ipados", "macos"}
    assert MANAGER_PLATFORMS == {"ios", "ipados", "macos", "web"}


def test_new_code_is_128_bits_base32():
    a, b = new_code("wc_"), new_code("wc_")
    assert re.fullmatch(r"wc_[a-z2-7]{26}", a)
    assert a != b


def test_new_secret_has_prefix_and_is_unique():
    a, b = new_secret("wcr_"), new_secret("wcr_")
    assert a.startswith("wcr_") and a != b
    assert len(base64.urlsafe_b64decode(a[4:] + "=" * (-len(a[4:]) % 4))) == 32


def test_secret_hash_is_sha256_hex():
    assert secret_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_account_id_shape():
    assert re.fullmatch(r"acc_[a-z2-7]{26}", new_account_id())
    assert new_account_id() != new_account_id()


def test_device_id_shape():
    assert re.fullmatch(r"dev_[0-9a-f]{16}", new_device_id())
    assert new_device_id() != new_device_id()


def test_parse_account_key_accepts_32_bytes():
    raw = bytes(range(32))
    assert parse_account_key(b64(raw)) == "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"
    assert len(b64(raw)) == 43


def _noncanonical() -> str:
    good = b64(bytes(32))  # 43 times "A"
    assert good.endswith("A")
    return good[:-1] + "B"  # same 32 bytes, but the 2 spare bits of the last char are not zero


@pytest.mark.parametrize(
    "value",
    [
        b64(bytes(31)),
        b64(bytes(33)),
        base64.urlsafe_b64encode(bytes(32)).decode(),  # padded
        "+" + b64(bytes(32))[1:],
        "/" + b64(bytes(32))[1:],
        12345,
        None,
        b"A" * 43,
        "",
        _noncanonical(),
    ],
)
def test_parse_account_key_rejects(value):
    with pytest.raises(ValueError, match="account_key must be 32 bytes in base64url") as e:
        parse_account_key(value)
    if isinstance(value, str) and value:
        assert value not in str(e.value)


def test_issue_and_verify_round_trip(tokens, key):
    token, exp = tokens.issue("acc_x", "dev_x", "ios")
    assert exp == int(NOW) + 3600
    assert tokens.verify(token) == AccountClaims(account="acc_x", device="dev_x", platform="ios", expires_at=exp)
    header = jwt.get_unverified_header(token)
    assert (header["typ"], header["kid"], header["alg"]) == (ACCOUNT_TOKEN_TYPE, key.kid, "ES256")
    body = jwt.decode(token, key.public_key, algorithms=["ES256"], audience=ISSUER, options={"verify_exp": False, "verify_iat": False})
    assert body["iss"] == body["aud"] == ISSUER
    assert (body["sub"], body["did"], body["plt"]) == ("acc_x", "dev_x", "ios")
    assert body["exp"] - body["iat"] == 3600
    assert re.fullmatch(r"[0-9a-f]{32}", body["jti"])


def test_ttl_is_configurable(key):
    t = AccountTokens(key, ISSUER, ttl_s=60, now=lambda: NOW)
    _, exp = t.issue("acc_x", "dev_x", "web")
    assert exp == int(NOW) + 60


def test_token_expires_after_leeway(key):
    token, _ = AccountTokens(key, ISSUER, now=lambda: NOW).issue("acc_x", "dev_x", "ios")
    inside = AccountTokens(key, ISSUER, now=lambda: NOW + 3600 + 30)
    assert inside.verify(token).account == "acc_x"
    late = AccountTokens(key, ISSUER, now=lambda: NOW + 3600 + 61)
    with pytest.raises(TokenError, match="account token expired"):
        late.verify(token)


def _hs256(**overrides):
    return jwt.encode(claims(**overrides), "x" * 32, algorithm="HS256", headers={"kid": "k", "typ": ACCOUNT_TOKEN_TYPE})


OTHER = new_key()


@pytest.mark.parametrize(
    "make",
    [
        lambda key: OTHER.sign(claims(), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(aud="https://elsewhere.test"), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(iss="https://elsewhere.test"), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(exp=int(NOW) - 3600), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(), typ="wc-server+jwt"),
        lambda key: key.sign(claims(did=None), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(sub=None), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(iat=None), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(exp=None), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(plt=None), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(plt="android"), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: key.sign(claims(did=7), typ=ACCOUNT_TOKEN_TYPE),
        lambda key: _hs256(),
        lambda key: "not a token",
        lambda key: "",
        lambda key: key.sign(claims(jti="x" * 5000), typ=ACCOUNT_TOKEN_TYPE),
    ],
    ids=[
        "other-key", "other-aud", "other-iss", "expired", "server-typ", "no-did", "no-sub", "no-iat", "no-exp",
        "no-plt", "unknown-plt", "did-not-str", "hs256", "garbage", "empty", "too-long",
    ],
)
def test_verify_rejects(tokens, key, make):
    token = make(key)
    with pytest.raises(TokenError) as e:
        tokens.verify(token)
    assert token not in str(e.value) or token == ""


def test_server_token_is_not_an_account_token(tokens, key):
    server = key.sign(
        server_token_claims(issuer=ISSUER, account="acc_x", client_id=None, audience=ISSUER, now=NOW)
    )
    with pytest.raises(TokenError, match="invalid account token"):
        tokens.verify(server)


def test_looks_like(tokens, key):
    token, _ = tokens.issue("acc_x", "dev_x", "ios")
    assert AccountTokens.looks_like(token) is True
    assert AccountTokens.looks_like(key.sign(claims())) is False  # wc-server+jwt
    assert AccountTokens.looks_like(_hs256()) is True  # only the header is looked at
    for junk in ("", "a.b.c", "not a token", "....", "x" * 5000, "e30.e30.e30"):
        assert AccountTokens.looks_like(junk) is False
    assert AccountTokens.looks_like(None) is False  # type: ignore[arg-type]
    assert AccountTokens.looks_like(b"abc") is False  # type: ignore[arg-type]


def test_real_clock_by_default(key):
    token, exp = AccountTokens(key, ISSUER).issue("acc_x", "dev_x", "ios")
    assert abs(exp - 3600 - time.time()) < 5
