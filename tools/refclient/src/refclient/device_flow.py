"""OAuth 2.0 Device Authorization Grant (RFC 8628) against the central account issuer."""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from . import __version__

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
USER_AGENT = f"wristcall-refclient/{__version__}"
SLOW_DOWN_STEP_S = 5
DEFAULT_INTERVAL_S = 5


class DeviceFlowError(Exception):
    pass


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


def _error_code(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return f"http_{r.status_code}"


def discover(issuer: str, http: httpx.Client) -> dict:
    """Reads the issuer's OpenID configuration; the device flow needs two of its endpoints."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        r = http.get(url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as e:
        raise DeviceFlowError(f"cannot reach the issuer: {type(e).__name__}") from e
    if r.status_code != 200:
        raise DeviceFlowError(f"issuer discovery failed ({r.status_code})")
    try:
        doc = r.json()
    except ValueError as e:
        raise DeviceFlowError("issuer discovery returned invalid JSON") from e
    if not isinstance(doc, dict):
        raise DeviceFlowError("issuer discovery returned invalid JSON")
    for key in ("device_authorization_endpoint", "token_endpoint"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            raise DeviceFlowError(f"the issuer does not advertise {key}: the device flow is not available")
    return doc


def start(issuer: str, client_id: str, scope: str, http: httpx.Client) -> tuple[DeviceCode, str]:
    """Asks for a device code. Returns it with the token endpoint to poll."""
    doc = discover(issuer, http)
    try:
        r = http.post(
            doc["device_authorization_endpoint"],
            data={"client_id": client_id, "scope": scope},
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as e:
        raise DeviceFlowError(f"cannot reach the issuer: {type(e).__name__}") from e
    if r.status_code != 200:
        raise DeviceFlowError(f"device authorization refused ({r.status_code}): {_error_code(r)}")
    try:
        body = r.json()
        code = DeviceCode(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            verification_uri_complete=body.get("verification_uri_complete"),
            expires_in=int(body["expires_in"]),
            interval=int(body.get("interval") or DEFAULT_INTERVAL_S),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise DeviceFlowError("device authorization returned an unexpected response") from e
    return code, doc["token_endpoint"]


def wait_for_token(
    token_endpoint: str,
    client_id: str,
    code: DeviceCode,
    http: httpx.Client,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Polls the token endpoint until the user approves (RFC 8628 sections 3.4 and 3.5)."""
    deadline = now() + code.expires_in
    interval = code.interval
    while True:
        try:
            r = http.post(
                token_endpoint,
                data={"grant_type": GRANT_TYPE, "device_code": code.device_code, "client_id": client_id},
                headers={"User-Agent": USER_AGENT},
            )
        except httpx.HTTPError as e:
            raise DeviceFlowError(f"cannot reach the issuer: {type(e).__name__}") from e
        if r.status_code == 200:
            try:
                tokens = r.json()
            except ValueError as e:
                raise DeviceFlowError("the token endpoint returned invalid JSON") from e
            if not isinstance(tokens, dict) or not tokens.get("access_token"):
                raise DeviceFlowError("the token endpoint returned no access token")
            return tokens
        error = _error_code(r)
        if error == "access_denied":
            raise DeviceFlowError("the login was denied")
        if error == "expired_token":
            raise DeviceFlowError("the login code expired: run login again")
        if error == "slow_down":
            interval += SLOW_DOWN_STEP_S
        elif error != "authorization_pending":
            raise DeviceFlowError(f"login failed: {error}")
        if now() + interval >= deadline:
            raise DeviceFlowError("the login code expired: run login again")
        sleep(interval)
