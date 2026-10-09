"""How history text is kept at rest and found again (design decision 16.5).

Text is stored plain, or sealed with AES-256-GCM when the operator sets `history.encryption_key`; a sealed text is
bound to its call and position (associated data), so it cannot be moved to another row unnoticed.

Search never hands user input to FTS5: the server cuts text into words itself (accents dropped, case folded,
letters and digits only) and indexes those words, or, with a key, a keyed hash of each word ("blind index").
The hashes reveal which entries share a word, not the word. A query is whole words, all required.
"""

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import unicodedata

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_BYTES = 32
NONCE_BYTES = 12
MAX_WORD = 64
MAX_QUERY_WORDS = 16
_WORD = re.compile(r"[^\W_]+")


class HistoryKeyError(Exception):
    """The key is malformed, missing for sealed text, or not the one that sealed it. Never carries the key."""


def parse_key(value: str) -> bytes:
    """32 bytes in base64 (standard or URL-safe, padding optional)."""
    text = value.strip().translate(str.maketrans("-_", "+/"))
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise HistoryKeyError("history.encryption_key must be 32 random bytes in base64") from None
    if len(raw) != KEY_BYTES:
        raise HistoryKeyError("history.encryption_key must be 32 random bytes in base64")
    return raw


def new_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode().rstrip("=")


def words(text: str) -> list[str]:
    """Searchable words: no accents, case folded, letters and digits only, each cut at 64 characters."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return [w[:MAX_WORD] for w in _WORD.findall(bare)]


def _derive(key: bytes, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"wristcall history " + purpose).derive(key)


class HistoryCodec:
    """Seals and opens history text; computes the terms to index and to query. key None: plain text."""

    def __init__(self, key: bytes | None = None) -> None:
        self._aead = AESGCM(_derive(key, b"text v1")) if key else None
        self._index_key = _derive(key, b"index v1") if key else None
        # Names the key without revealing it: stored in meta so that a server started with another key refuses to run.
        self.key_id = _derive(key, b"key id")[:8].hex() if key else None

    @property
    def encrypted(self) -> bool:
        return self._aead is not None

    def seal(self, text: str, aad: str) -> tuple[str, bool]:
        """(stored text, sealed?)."""
        if self._aead is None:
            return text, False
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, text.encode(), aad.encode())
        return base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("="), True

    def open(self, stored: str, sealed: bool, aad: str) -> str:
        if not sealed:
            return stored
        if self._aead is None:
            raise HistoryKeyError("this history is encrypted: set history.encryption_key")
        try:
            raw = base64.urlsafe_b64decode(stored + "=" * (-len(stored) % 4))
            return self._aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad.encode()).decode()
        except (InvalidTag, ValueError, binascii.Error):
            raise HistoryKeyError("a history entry cannot be decrypted (another key, or damaged)") from None

    def _hash(self, word: str) -> str:
        assert self._index_key is not None
        return "x" + hmac.new(self._index_key, word.encode(), hashlib.sha256).hexdigest()[:16]

    def index_terms(self, text: str) -> list[str]:
        """Terms stored in the search index for this text: its distinct words, or their hashes."""
        distinct = list(dict.fromkeys(words(text)))
        return [self._hash(w) for w in distinct] if self._index_key else distinct

    def query_terms(self, query: str) -> list[list[str]]:
        """One group per distinct query word (at most 16); an entry matches when it has a term of every group.

        With a key, a group also holds the plain word: entries written before the key was set stay findable
        until `wristcall history encrypt` seals them.
        """
        distinct = list(dict.fromkeys(words(query)))[:MAX_QUERY_WORDS]
        if self._index_key is None:
            return [[w] for w in distinct]
        return [[w, self._hash(w)] for w in distinct]
