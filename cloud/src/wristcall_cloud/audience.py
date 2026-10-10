"""Token audiences: the URL a client connects to, in one canonical form.

The Cloud mints per-server tokens bound to this form and the server checks it, so both sides must agree byte for
byte: server/src/wristcall/audience.py is an exact copy of cloud/src/wristcall_cloud/audience.py (CI compares them).
Standard library only. Errors never carry the URL: it says where a user has a server.
"""

import ipaddress
import re
from urllib.parse import urlsplit

MAX_LENGTH = 2048
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
DEFAULT_PORTS = {"https": 443, "http": 80}
_HOST = re.compile(r"[a-z0-9.-]+")
_HEX_LABEL = re.compile(r"0x[0-9a-f]*")
_PATH = re.compile(r"(/[A-Za-z0-9._~-]+)*/?")


class AudienceError(ValueError):
    pass


def normalize_audience(url: str) -> str:
    """The URL a client connects to, as a token audience: scheme and host lowercased, default port dropped,
    no trailing slash, path kept. https only (http only for loopback hosts); no user, password, query or fragment."""
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_LENGTH:
        raise AudienceError(f"audience must be a URL of 1 to {MAX_LENGTH} characters")
    # Before parsing: urlsplit silently drops tabs and newlines and takes a backslash as part of the host, and
    # some non-ASCII letters lowercase to ASCII ones (KELVIN SIGN to "k").
    if not url.isascii() or any(c.isspace() or not c.isprintable() or c == "\\" for c in url):
        raise AudienceError(
            "audience must be ASCII (names in punycode), without spaces, control characters or backslashes"
        )
    if "?" in url or "#" in url:
        raise AudienceError("audience must not have a query or a fragment")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise AudienceError("audience is not a valid URL") from None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise AudienceError("audience must be an https URL")
    if "@" in parts.netloc:
        raise AudienceError("audience must not have a user or a password")
    host = _host(parts.netloc)
    if scheme == "http" and not _loopback_host(host):
        raise AudienceError("audience must be an https URL (http only for loopback hosts)")
    try:
        port = parts.port
    except ValueError:
        raise AudienceError("audience has an invalid port") from None
    if port == 0:
        raise AudienceError("audience has an invalid port")
    path = parts.path
    if not _PATH.fullmatch(path) or any(segment in (".", "..") for segment in path.split("/")):
        raise AudienceError("audience has an invalid path")
    path = path.removesuffix("/")
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
    return f"{scheme}://{netloc}{path}"


def is_loopback(audience: str) -> bool:
    """Whether an audience (already normalized) points at this machine."""
    return _loopback_host(_host(urlsplit(audience).netloc))


def _loopback_host(host: str) -> bool:
    """localhost or a loopback address: 127.0.0.0/8, ::1 and IPv4-mapped 127.x (host as `_host` returns it)."""
    if host in LOOPBACK_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return False
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def _host(netloc: str) -> str:
    """The host of a netloc without user info: lowercase ASCII name, or an IPv6 address in brackets."""
    if netloc.startswith("["):
        end = netloc.find("]")
        literal = netloc[1:end] if end > 0 else ""
        rest = netloc[end + 1 :] if end > 0 else ""
        if not literal or "%" in literal or (rest and not rest.startswith(":")):
            raise AudienceError("audience has an invalid host")
        try:
            address = ipaddress.IPv6Address(literal)
        except ValueError:
            raise AudienceError("audience has an invalid host") from None
        # Python 3.13 changed how IPv4-mapped addresses print: spell them out so every version agrees.
        if address.ipv4_mapped is not None:
            return f"[::ffff:{address.ipv4_mapped}]"
        return f"[{address.compressed}]"
    host = netloc.rpartition(":")[0] if ":" in netloc else netloc
    host = host.lower()
    if not _HOST.fullmatch(host) or host.startswith(".") or host.endswith(".") or ".." in host:
        raise AudienceError("audience has an invalid host (non-ASCII names must be in punycode)")
    # A host ending in a number is an IPv4 address to browsers and resolvers, which also read 127.1, 0x7f.1 or
    # 2130706433 as 127.0.0.1: only the canonical a.b.c.d form (no leading zeros) is accepted.
    last = host.rpartition(".")[2]
    if last.isdigit() or _HEX_LABEL.fullmatch(last):
        try:
            canonical = str(ipaddress.IPv4Address(host)) == host
        except ValueError:
            canonical = False
        if not canonical:
            raise AudienceError("audience has an invalid host (IPv4 addresses as a.b.c.d)")
    return host
