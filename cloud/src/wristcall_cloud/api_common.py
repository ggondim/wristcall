"""What every route module shares: the API error, the JSON body, the client address and the `device` body."""

from typing import Any

from fastapi import Request

from .session_tokens import PLATFORMS

MAX_DEVICE_NAME = 64
MAX_PUBLIC_KEY = 1024


class ApiError(Exception):
    """Answered as `{"error": code, "message": message}`; `retry_after` (seconds) becomes a Retry-After header."""

    def __init__(self, code: str, message: str, status: int, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after


async def json_object(request: Request) -> dict[str, Any]:
    """The request body as a JSON object (422 `invalid` otherwise); size already capped by BodyLimit."""
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        raise ApiError("invalid", "body must be valid JSON", 422) from None
    if not isinstance(body, dict):
        raise ApiError("invalid", "body must be a JSON object", 422)
    return body


def client_ip(request: Request, header: str | None) -> str:
    """The client address: the last value of the proxy's header when one is configured (what the proxy itself
    appended, on the same line or on a line of its own; anything before it came from the client), the connection's
    address otherwise."""
    if header:
        value = ",".join(request.headers.getlist(header)).rpartition(",")[2].strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"


def parse_device(value: Any) -> dict[str, Any]:
    """The `device` of a body that creates a device: name and platform, plus public_key when given. Unknown fields
    (`attestation` among them, phase A) are ignored."""
    if not isinstance(value, dict):
        raise ApiError("invalid", "device must be an object", 422)
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_DEVICE_NAME:
        raise ApiError("invalid", f"device.name must be a string of 1 to {MAX_DEVICE_NAME} characters", 422)
    platform = value.get("platform")
    if platform not in PLATFORMS:
        raise ApiError("invalid", f"device.platform must be one of {', '.join(PLATFORMS)}", 422)
    device: dict[str, Any] = {"name": name, "platform": platform}
    public_key = value.get("public_key")
    if public_key is not None:
        if not isinstance(public_key, str) or not public_key or len(public_key) > MAX_PUBLIC_KEY:
            raise ApiError("invalid", f"device.public_key must be a string of at most {MAX_PUBLIC_KEY} characters", 422)
        device["public_key"] = public_key
    return device
