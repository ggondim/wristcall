"""The `X-Wristcall-Client: <platform>/<x.y.z>` header: which app, in which version, is calling."""

import re
from collections.abc import Mapping

CLIENT_HEADER = "x-wristcall-client"
_CLIENT = re.compile(r"(ios|ipados|macos|watchos|web)/(\d{1,4})\.(\d{1,4})\.(\d{1,4})")


def parse_client(value: str | None) -> tuple[str, tuple[int, int, int]] | None:
    """(platform, (major, minor, patch)) of a well-formed header value, else None."""
    if value is None:
        return None
    match = _CLIENT.fullmatch(value.strip())
    if match is None:
        return None
    return match[1], (int(match[2]), int(match[3]), int(match[4]))


def sees_cloud_entry(header: str | None, platform: str, minimums: Mapping[str, tuple[int, int, int]]) -> bool:
    """Whether a caller sees the managed Cloud entry: a valid header whose platform is the token's `platform`,
    is listed in `minimums` and whose version reaches that minimum."""
    client = parse_client(header)
    if client is None:
        return False
    client_platform, version = client
    minimum = minimums.get(client_platform)
    return client_platform == platform and minimum is not None and version >= minimum
