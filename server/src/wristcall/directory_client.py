"""Client for the pairing directory (8 digit code → server URL)."""

import httpx


class DirectoryError(Exception):
    pass


class CodeConflict(DirectoryError):
    pass


class DirectoryClient:
    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self._url = base_url.rstrip("/") + "/v1/codes"
        self._http = http or httpx.Client(timeout=10.0)

    def register(self, server_url: str, code: str) -> None:
        try:
            r = self._http.post(self._url, json={"url": server_url, "code": code})
        except httpx.HTTPError as e:
            raise DirectoryError(f"directory unreachable: {e}") from e
        if r.status_code == 409:
            raise CodeConflict(code)
        if r.status_code >= 400:
            raise DirectoryError(f"directory responded {r.status_code}: {r.text[:200]}")
