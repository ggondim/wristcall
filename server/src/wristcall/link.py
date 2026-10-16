"""Long, single-use link codes: how an app or the PWA links a Cloud account to a user of this server without typing.

The code travels in a QR or a link; only its SHA-256 is stored (like pairing and API tokens). It is separate from
the 8-digit pairing code, which stays short for people to read and type.

The server shows the QR in its startup log while it is new (nobody linked, a single user who has no API token and
no device yet); `wristcall link [--user]` shows one at any time. Link URL format: docs/protocol.md, "Link URL".
"""

import base64
import io
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import segno

from .audience import AudienceError, normalize_audience
from .bootstrap import create_owner
from .config import AppConfig
from .pairing import hash_secret
from .storage import Storage, User
from .users import UserError, UserService

LINK_TTL_S = 900  # a link code lives 15 minutes
# The PWA page that opens the app or links in the browser. A constant, not a setting: another Cloud changes it here.
LINK_PAGE = "https://wristcall-app.trigram.com.br/link"
STARTUP_AGAIN = "docker compose exec wristcall wristcall link"
CLI_AGAIN = "wristcall link"

NO_ACCOUNT_HINT = (
    "To link this server to the wristcall app with a QR code, set central_account in wristcall.yaml "
    '(see the README, "Central account").'
)
UNLINKED = "No user of this server is linked to a wristcall account yet."


def new_link_code() -> str:
    """16 random bytes in base32 (RFC 4648, upper case, no padding): 26 characters of [A-Z2-7]."""
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")


def normalize_link_code(raw: str) -> str | None:
    """Strips spaces and hyphens, upper-cases; None if what is left is not a link code."""
    cleaned = re.sub(r"[\s-]", "", raw).upper()
    return cleaned if re.fullmatch(r"[A-Z2-7]{26}", cleaned) else None


def link_url(public_url: str, code: str) -> str:
    """Everything goes in the fragment, which browsers never send to the page's server."""
    return f"{LINK_PAGE}#v=1&s={quote(public_url, safe=':/')}&c={code}"


def canonical_public_url(config: AppConfig) -> str:
    """`server.public_url` in the form the Cloud and this server compare audiences in (as given if it has none)."""
    try:
        return normalize_audience(config.server.public_url)
    except AudienceError:
        return config.server.public_url


def public_url_in_audience(config: AppConfig) -> bool:
    """Whether an app reaching the server at `public_url` gets Cloud tokens this server accepts."""
    if config.central_account is None:
        return False
    try:
        return normalize_audience(config.server.public_url) in config.central_account.audience
    except AudienceError:
        return False


@dataclass(frozen=True)
class LinkInvite:
    user: User
    code: str
    url: str
    expires_at: float


class LinkService:
    def __init__(self, storage: Storage, public_url: str, now: Callable[[], float] = time.time) -> None:
        self._st = storage
        self._public_url = public_url
        self._now = now

    async def issue(self, user_id: str) -> LinkInvite:
        user = await self._st.users.get(user_id)
        if user is None:
            raise UserError("user not found")
        now = self._now()
        await self._st.pairing.purge(now)
        # Only the newest QR works: a code left in an earlier startup log must not link this user later.
        await self._st.pairing.discard_link_codes(user.id)
        code = new_link_code()
        expires_at = now + LINK_TTL_S
        await self._st.pairing.add_link_code(hash_secret(code), user.id, expires_at)
        return LinkInvite(user, code, link_url(self._public_url, code), expires_at)


def render_invite(invite: LinkInvite, again: str, user_line: bool = False) -> str:
    """The QR box. Compact drawing: blocks are the light modules, so on a light terminal the QR comes out
    inverted (some readers refuse it); the link printed below works anyway."""
    qr = io.StringIO()
    segno.make(invite.url, error="m").terminal(out=qr, compact=True, border=2)
    lines = ["Connect this server to your iPhone"]
    if user_line:
        lines.append(f"For user {invite.user.handle}.")
    lines += [
        "Scan this QR code with your iPhone camera:",
        qr.getvalue().rstrip("\n"),
        "Or open this link on your iPhone:",
        invite.url,
        f"Works once, for {LINK_TTL_S // 60} minutes. New code: {again}",
    ]
    return "\n".join(lines)


async def target_user(storage: Storage, users: UserService, handle: str | None) -> User:
    """The user a link code is for: `handle`, or the only user; a server without users gets `owner`."""
    if handle is None and not await storage.users.list():
        user, _ = await create_owner(storage, time.time())
        return user
    return await users.resolve(handle)


async def _in_use(storage: Storage, user: User) -> bool:
    return bool(await storage.tokens.list(user.id) or await storage.devices.list(user.id))


async def startup_invite(
    storage: Storage, config: AppConfig, users: UserService, links: LinkService | None
) -> str | None:
    """What the startup log shows about linking, or None. A code only on a new server: the code gives the owner's
    API token, so a server already in use never leaves one in its logs."""
    listed = await storage.users.list()
    if config.central_account is None:
        new = not listed or (len(listed) == 1 and not await _in_use(storage, listed[0]))
        return NO_ACCOUNT_HINT if new else None
    if not public_url_in_audience(config):
        return None  # the app's token would be refused; create_app already warns about it
    if any(u.central_subject for u in listed):
        return None
    if len(listed) > 1:
        return f"{UNLINKED} To link one with a QR code: {STARTUP_AGAIN} --user <handle>"
    user = listed[0] if listed else await target_user(storage, users, None)
    if await _in_use(storage, user):
        return f"{UNLINKED} To link it with a QR code: {STARTUP_AGAIN}"
    links = links or LinkService(storage, canonical_public_url(config))
    return render_invite(await links.issue(user.id), STARTUP_AGAIN)
