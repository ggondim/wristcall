import base64
import json
import time

import httpx
import jwt
import pytest
import respx

from oidc_fixtures import AUDIENCE, ISSUER, FakeIssuer, ec_key, rsa_key
from wristcall_cloud.oidc import OidcError, OidcUnavailable, OidcVerifier


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def issuer(router):
    return FakeIssuer(router)


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


def verifier(clock=time.time, audiences=(AUDIENCE,), issuer_url=ISSUER) -> OidcVerifier:
    return OidcVerifier(issuer_url, list(audiences), httpx.AsyncClient(), now=clock)


async def test_valid_token_gives_identity(issuer):
    identity = await verifier().verify(issuer.token())
    assert identity.subject == "central-user-1"
    assert identity.issuer == ISSUER
    assert identity.client_id == AUDIENCE


async def test_issuer_with_trailing_slash_is_normalized(issuer):
    assert (await verifier(issuer_url=ISSUER + "/").verify(issuer.token())).subject == "central-user-1"


async def test_any_configured_audience_is_accepted(issuer):
    v = verifier(audiences=("project-id", "other"))
    assert (await v.verify(issuer.token(aud=["client-x", "project-id"]))).subject == "central-user-1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": ["someone-else"]},
        {"iss": "https://evil.test"},
        {"exp": time.time() - 3600},
        {"nbf": time.time() + 3600},
        {"sub": None},
        {"exp": None},
        {"iat": None},
        {"aud": None},
        {"sub": ""},
        {"sub": "x" * 300},
    ],
)
async def test_bad_claims_are_rejected(issuer, overrides):
    with pytest.raises(OidcError):
        await verifier().verify(issuer.token(**overrides))


async def test_small_clock_skew_is_tolerated(issuer):
    assert await verifier().verify(issuer.token(exp=time.time() - 30))


async def test_typ_is_enforced_when_set(issuer):
    typed = OidcVerifier(ISSUER, [AUDIENCE], httpx.AsyncClient(), typ="wc-server+jwt")
    assert (await typed.verify(issuer.token(headers={"typ": "wc-server+jwt"}))).subject == "central-user-1"
    for headers in ({"typ": "JWT"}, {"typ": "at+jwt"}, {"typ": "WC-SERVER+JWT"}, {"typ": None}):
        with pytest.raises(OidcError, match="unexpected token type"):
            await typed.verify(issuer.token(headers=headers))
    # Without `typ` the header is not looked at (the Cloud verifying the issuer's own access tokens).
    assert (await verifier().verify(issuer.token(headers={"typ": "at+jwt"}))).subject == "central-user-1"


@pytest.mark.parametrize("overrides", [{"iat": time.time() + 3600}, {"nbf": time.time() + 3600}])
async def test_token_from_the_future_points_at_the_clock(issuer, overrides):
    with pytest.raises(OidcError, match="token not valid yet; check the server clock"):
        await verifier().verify(issuer.token(**overrides))


async def test_signature_from_another_key_is_rejected(issuer):
    token = issuer.token()
    other = jwt.encode(jwt.decode(token, options={"verify_signature": False}), rsa_key(), "RS256", headers={"kid": "k1"})
    with pytest.raises(OidcError):
        await verifier().verify(other)


def _unsigned(header: dict, claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc(header)}.{enc(claims)}."


async def test_alg_none_is_rejected(issuer):
    claims = {"iss": ISSUER, "sub": "s", "aud": [AUDIENCE], "iat": time.time(), "exp": time.time() + 60}
    with pytest.raises(OidcError):
        await verifier().verify(_unsigned({"alg": "none", "kid": "k1"}, claims))
    assert issuer.jwks.call_count == 0


async def test_hmac_with_public_key_is_rejected(issuer):
    # Classic confusion attack: HS256 signed with the RSA public key as the shared secret.
    claims = {"iss": ISSUER, "sub": "s", "aud": [AUDIENCE], "iat": time.time(), "exp": time.time() + 60}
    token = jwt.encode(claims, "public-key-bytes", algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(OidcError):
        await verifier().verify(token)


async def test_alg_must_match_the_key(issuer):
    issuer.keys["e1"] = (ec_key(), "ES256")
    token = issuer.token("e1")
    assert await verifier().verify(token)
    # Same EC key announced as RS256 by the header: refused before decoding.
    header, rest = token.split(".", 1)
    forged = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "e1"}).encode()).rstrip(b"=").decode()
    with pytest.raises(OidcError):
        await verifier().verify(f"{forged}.{rest}")


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c.d", "x" * 9000, "a.b.c"])
async def test_malformed_tokens_are_rejected_without_network(issuer, token):
    with pytest.raises(OidcError):
        await verifier().verify(token)
    assert issuer.discovery.call_count == 0


async def test_token_without_kid_is_rejected(issuer):
    claims = {"iss": ISSUER, "sub": "s", "aud": [AUDIENCE], "iat": time.time(), "exp": time.time() + 60}
    token = jwt.encode(claims, issuer.keys["k1"][0], algorithm="RS256")
    with pytest.raises(OidcError):
        await verifier().verify(token)


async def test_errors_never_echo_the_token(issuer):
    token = issuer.token(aud=["someone-else"])
    with pytest.raises(OidcError) as e:
        await verifier().verify(token)
    assert token not in str(e.value) and token[:20] not in str(e.value)


async def test_keys_are_cached(issuer):
    v = verifier()
    for _ in range(5):
        await v.verify(issuer.token())
    assert issuer.jwks.call_count == 1


async def test_rotated_key_is_fetched_once(issuer):
    clock = Clock()
    v = verifier(clock)
    await v.verify(issuer.token())
    issuer.keys["k2"] = (rsa_key(), "RS256")
    clock.t += 61
    assert await v.verify(issuer.token("k2"))
    assert issuer.jwks.call_count == 2


async def test_unknown_kids_do_not_amplify_requests(issuer):
    clock = Clock()
    v = verifier(clock)
    await v.verify(issuer.token())
    issuer.keys["k9"] = (rsa_key(), "RS256")
    forged = issuer.token("k9")
    del issuer.keys["k9"]
    for _ in range(20):
        with pytest.raises(OidcError):
            await v.verify(forged)
    # Within refetch_min_s of the last fetch: no refetch at all.
    assert issuer.jwks.call_count == 1
    clock.t += 61
    with pytest.raises(OidcError):
        await v.verify(forged)
    assert issuer.jwks.call_count == 2


async def test_keys_expire_after_ttl(issuer):
    clock = Clock()
    v = verifier(clock)
    await v.verify(issuer.token())
    clock.t += 3601
    await v.verify(issuer.token())
    assert issuer.jwks.call_count == 2


async def test_issuer_down_without_cache_is_unavailable(router):
    router.get(f"{ISSUER}/.well-known/openid-configuration").mock(side_effect=httpx.ConnectError("down"))
    token = FakeIssuer(respx.mock(assert_all_called=False)).token()
    with pytest.raises(OidcUnavailable):
        await verifier().verify(token)


async def test_issuer_down_with_stale_cache_still_verifies(issuer):
    clock = Clock()
    v = verifier(clock)
    await v.verify(issuer.token())
    issuer.discovery.side_effect = httpx.ConnectError("down")
    clock.t += 7200
    assert (await v.verify(issuer.token())).subject == "central-user-1"


@pytest.mark.parametrize(
    "discovery",
    [
        {"issuer": "https://evil.test", "jwks_uri": f"{ISSUER}/oauth/v2/keys"},
        {"issuer": ISSUER},
        {"issuer": ISSUER, "jwks_uri": "http://issuer.test/oauth/v2/keys"},
        ["not", "an", "object"],
    ],
)
async def test_bad_discovery_is_unavailable(issuer, discovery):
    issuer.discovery.side_effect = lambda request: httpx.Response(200, json=discovery)
    with pytest.raises(OidcUnavailable):
        await verifier().verify(issuer.token())


async def test_empty_or_broken_jwks_is_unavailable(issuer):
    issuer.jwks.side_effect = lambda request: httpx.Response(200, json={"keys": [{"kid": "x", "kty": "RSA"}]})
    with pytest.raises(OidcUnavailable):
        await verifier().verify(issuer.token())


async def test_encryption_keys_are_ignored(issuer):
    issuer.jwks.side_effect = lambda request: httpx.Response(
        200, json={"keys": [{**__import__("oidc_fixtures").public_jwk(issuer.keys["k1"][0], "k1"), "use": "enc"}]}
    )
    with pytest.raises(OidcUnavailable):
        await verifier().verify(issuer.token())


async def test_requests_carry_a_server_user_agent(issuer):
    # auth.trigram.com.br (Cloudflare) refuses Python-urllib's user agent with error 1010.
    await verifier().verify(issuer.token())
    assert issuer.jwks.calls.last.request.headers["user-agent"].startswith("wristcall-cloud/")


def test_verifier_needs_an_audience():
    with pytest.raises(ValueError):
        OidcVerifier(ISSUER, [], httpx.AsyncClient())


async def test_issuer_outage_does_not_amplify_requests(router):
    clock = Clock()
    discovery = router.get(f"{ISSUER}/.well-known/openid-configuration").mock(side_effect=httpx.ConnectError("down"))
    token = FakeIssuer(respx.mock(assert_all_called=False)).token()
    v = verifier(clock)
    for _ in range(50):
        with pytest.raises(OidcUnavailable):
            await v.verify(token)
    assert discovery.call_count == 1
    clock.t += 61
    with pytest.raises(OidcUnavailable):
        await v.verify(token)
    assert discovery.call_count == 2


async def test_issuer_back_after_outage(issuer):
    clock = Clock()
    v = verifier(clock)
    issuer.discovery.side_effect = httpx.ConnectError("down")
    with pytest.raises(OidcUnavailable):
        await v.verify(issuer.token())
    issuer.discovery.side_effect = lambda request: httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/oauth/v2/keys"})
    clock.t += 61
    assert await v.verify(issuer.token())


@pytest.mark.parametrize("claim", [{"nonce": "n"}, {"at_hash": "h"}])
async def test_id_tokens_are_rejected(issuer, claim):
    with pytest.raises(OidcError):
        await verifier().verify(issuer.token(**claim))


async def test_clients_restrict_who_the_token_was_issued_to(issuer):
    v = OidcVerifier(ISSUER, ["project-id", AUDIENCE], httpx.AsyncClient(), clients=[AUDIENCE])
    assert await v.verify(issuer.token(aud=["project-id"], client_id=AUDIENCE))
    with pytest.raises(OidcError):
        await v.verify(issuer.token(aud=["project-id"], client_id="other-app"))
    with pytest.raises(OidcError):
        await v.verify(issuer.token(aud=["project-id"], client_id=None))
    assert await v.verify(issuer.token(aud=["project-id"], client_id=None, azp=AUDIENCE))
