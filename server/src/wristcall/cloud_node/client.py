"""The node's HTTP client for the Wristcall Cloud's internal API (`/v1/node/*`, see `cloud/README.md`, "Cloud nodes").

Every request carries the node's secret; the routes that act for a user also carry the user's per-server token
(`UserProof`), and the routes that work on one call carry the call grant the Cloud issued when the call opened.

Whatever is not a usable answer becomes `CloudUnavailable`: a transport error or timeout, a 5xx, a body that is not
JSON. A 4xx comes back as a `CloudResponse` with its error code, for the caller to map (`raise_for_account` for the
codes every account route shares). Neither the secret, a token, a grant nor a body ever reaches a log or a message.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from ..storage.models import CloudUnavailable, DeviceRevoked, PlanRequired
from .proof import UserProof

log = logging.getLogger("wristcall.cloud_node.client")

USER_TOKEN_HEADER = "X-Wristcall-User-Token"
GRANT_HEADER = "X-Wristcall-Call-Grant"


@dataclass(frozen=True)
class CloudResponse:
    status: int
    body: Any  # the decoded JSON, None for an empty body (204, 304)
    headers: httpx.Headers

    @property
    def code(self) -> str | None:
        """The Cloud's error code (`{"error": code, "message": ...}`), None when the body has none."""
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), str):
            return self.body["error"]
        return None


def _is_json(content_type: str, *, ndjson: bool = False) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json") or (ndjson and media == "application/x-ndjson")


class CloudClient:
    def __init__(self, base_url: str, node_secret: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._secret = node_secret
        self._http = http  # its timeouts are the client's (`create_app` gives it `httpx.Timeout(10.0, connect=5.0)`)

    def __repr__(self) -> str:
        return f"CloudClient({self._base!r})"

    def _headers(self, user: UserProof | None, grant: str | None, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {**(extra or {}), "Authorization": f"Bearer {self._secret}"}
        if user is not None:
            headers[USER_TOKEN_HEADER] = user.token
        if grant is not None:
            headers[GRANT_HEADER] = grant
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        user: UserProof | None = None,
        grant: str | None = None,
        json: Any = None,
        params: list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        lenient_body: bool = False,
    ) -> CloudResponse:
        """One request to the Cloud. 2xx, 3xx and 4xx answers come back; anything else raises `CloudUnavailable`.
        `lenient_body`: for a 2xx the status is what matters (the outbox), so a body that is not JSON comes back as
        `None` instead of raising. Any other status with such a body (a proxy's HTML 4xx during a deploy) still
        raises: it is not the Cloud talking."""
        try:
            resp = await self._http.request(
                method, self._base + path, json=json, params=params, headers=self._headers(user, grant, headers)
            )
        except httpx.HTTPError as e:  # transport errors and timeouts; their text may hold the URL, so only the type
            log.warning("Wristcall Cloud unreachable on %s %s (%s)", method, path, type(e).__name__)
            raise CloudUnavailable(f"Wristcall Cloud unreachable ({type(e).__name__})") from None
        if resp.status_code >= 500:
            log.warning("Wristcall Cloud answered %d on %s %s", resp.status_code, method, path)
            raise CloudUnavailable(f"Wristcall Cloud answered {resp.status_code}")
        body: Any = None
        if resp.content:
            if not _is_json(resp.headers.get("content-type", "")):
                if lenient_body and 200 <= resp.status_code < 300:
                    return CloudResponse(status=resp.status_code, body=None, headers=resp.headers)
                log.warning("Wristcall Cloud answered %d without JSON on %s %s", resp.status_code, method, path)
                raise CloudUnavailable("Wristcall Cloud answered without JSON")
            try:
                body = resp.json()
            except ValueError:
                if lenient_body and 200 <= resp.status_code < 300:
                    return CloudResponse(status=resp.status_code, body=None, headers=resp.headers)
                log.warning("Wristcall Cloud answered %d with invalid JSON on %s %s", resp.status_code, method, path)
                raise CloudUnavailable("Wristcall Cloud answered invalid JSON") from None
        return CloudResponse(status=resp.status_code, body=body, headers=resp.headers)

    async def stream_lines(
        self, path: str, *, user: UserProof, params: list[tuple[str, str]]
    ) -> AsyncIterator[dict]:
        """The objects of an NDJSON answer (the export), one per line. The Cloud ends a complete stream with
        `{"end": true, "count": n}`, which is checked and not yielded: a stream cut before it raises
        `CloudUnavailable` after the lines that did arrive."""
        count = 0
        try:
            async with self._http.stream(
                "GET", self._base + path, params=params, headers=self._headers(user, None, None)
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    if resp.status_code < 500 and resp.content and _is_json(resp.headers.get("content-type", "")):
                        try:
                            body = resp.json()
                        except ValueError:
                            body = None
                        raise_for_account(CloudResponse(status=resp.status_code, body=body, headers=resp.headers))
                    log.warning("Wristcall Cloud answered %d on GET %s", resp.status_code, path)
                    raise CloudUnavailable(f"Wristcall Cloud answered {resp.status_code}")
                if not _is_json(resp.headers.get("content-type", ""), ndjson=True):
                    log.warning("Wristcall Cloud answered without NDJSON on GET %s", path)
                    raise CloudUnavailable("Wristcall Cloud answered without NDJSON")
                async for raw in resp.aiter_lines():
                    if not raw.strip():
                        continue
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        line = None
                    if not isinstance(line, dict):
                        log.warning("Wristcall Cloud sent a line that is not a JSON object on GET %s", path)
                        raise CloudUnavailable("Wristcall Cloud sent a line that is not a JSON object")
                    if line.get("end") is True:
                        if line.get("count") != count:
                            log.warning("Wristcall Cloud stream on GET %s ended with a wrong count", path)
                            raise CloudUnavailable("Wristcall Cloud stream ended with a wrong count")
                        return
                    count += 1
                    yield line
        except httpx.HTTPError as e:
            log.warning("Wristcall Cloud stream on GET %s broke (%s)", path, type(e).__name__)
            raise CloudUnavailable(f"Wristcall Cloud stream broke ({type(e).__name__})") from None
        log.warning("Wristcall Cloud stream on GET %s was cut before its end line", path)
        raise CloudUnavailable("Wristcall Cloud stream was cut before its end line")


def raise_for_account(resp: CloudResponse) -> None:
    """Raises for the answers every account route shares: no plan (`PlanRequired`), a removed device
    (`DeviceRevoked`), and a refused credential or a rate limit (`CloudUnavailable`: nothing the user can fix)."""
    if resp.status == 402 and resp.code == "plan_required":
        raise PlanRequired("this account has no Wristcall Cloud plan")
    if resp.status == 403 and resp.code == "device_revoked":
        raise DeviceRevoked("this device was removed from the account")
    if resp.status == 401:
        log.error("Wristcall Cloud refused the node credential or the user token (401)")
        raise CloudUnavailable("Wristcall Cloud refused the node credential or the user token")
    if resp.status == 429:
        log.warning("Wristcall Cloud rate limited this node (Retry-After %s)", resp.headers.get("retry-after"))
        raise CloudUnavailable("Wristcall Cloud rate limited this node")
