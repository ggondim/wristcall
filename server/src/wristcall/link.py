"""Long, single-use link codes: how an app or the PWA links a Cloud account to a user of this server without typing.

The code travels in a QR or a link; only its SHA-256 is stored (like pairing and API tokens). It is separate from
the 8-digit pairing code, which stays short for people to read and type.
"""

import re
import secrets

LINK_TTL_S = 900  # a link code lives 15 minutes
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"  # base32: no 0/1/8/9, so it survives being read aloud or retyped
_LENGTH = 26  # 26 * 5 bits = 130 bits


def new_link_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def normalize_link_code(raw: str) -> str | None:
    """Strips spaces and hyphens, upper-cases; None if what is left is not a link code."""
    cleaned = re.sub(r"[\s-]", "", raw).upper()
    return cleaned if re.fullmatch(r"[A-Z2-7]{26}", cleaned) else None
