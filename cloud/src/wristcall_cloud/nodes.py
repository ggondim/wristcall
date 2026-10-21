"""The registry of wristcall Cloud nodes: the servers the Cloud itself vouches for.

A node authenticates with a secret (`wc_node_...`) the operator receives once, at `add` or `rotate`; only its
SHA-256 digest is stored. Rotation keeps the previous secret for a grace period, since a node deploy is not atomic.
Errors never carry the secret or the URL.
"""

import re
import secrets
import time
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from .audience import is_loopback, normalize_audience
from .cloud_store import CloudModeStore
from .session_tokens import secret_hash

NODE_SECRET_PREFIX = "wc_node_"
KINDS = ("always_on", "on_demand")
CAPABILITIES = ("operator_stt", "operator_tts")
ROTATION_GRACE_S = 900
_SECRET = re.compile(r"wc_node_[A-Za-z0-9_-]{43}")
_NODE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
_REGION = re.compile(r"[a-z]{3}")


class NodeExists(Exception):
    """The node id or the URL is already registered."""


def new_node_secret() -> str:
    return NODE_SECRET_PREFIX + secrets.token_urlsafe(32)


def is_node_secret(value: str) -> bool:
    """Whether a credential has the shape of a node secret (checked before any database lookup)."""
    return isinstance(value, str) and _SECRET.fullmatch(value) is not None


def parse_node_id(value: str) -> str:
    if not isinstance(value, str) or not _NODE_ID.fullmatch(value):
        raise ValueError("node id must be 1 to 32 lowercase letters, digits or hyphens, starting with a letter")
    return value


def parse_region(value: str) -> str:
    if not isinstance(value, str) or not _REGION.fullmatch(value):
        raise ValueError("region must be three lowercase letters")
    return value


def parse_node_url(value: str, *, allow_loopback: bool) -> str:
    """The node URL in audience form (the same form a token for it would be bound to); https only, or a loopback
    host when `allow_loopback`."""
    url = normalize_audience(value)  # AudienceError is a ValueError
    if is_loopback(url) and not allow_loopback:
        raise ValueError("node URL must not be a loopback address")
    return url


class NodeStore:
    def __init__(self, cloud: CloudModeStore) -> None:
        self.cloud = cloud

    async def add(
        self,
        node_id: str,
        *,
        url: str,
        region: str,
        kind: str,
        capabilities: list[str],
        priority: int = 100,
        allow_loopback: bool = False,
        now: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Registers a node; returns its document and its secret (the only time it exists in clear)."""
        if kind not in KINDS:
            raise ValueError("kind must be always_on or on_demand")
        if any(c not in CAPABILITIES for c in capabilities):
            raise ValueError("unknown capability")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("priority must be an integer")
        secret = new_node_secret()
        doc: dict[str, Any] = {
            "_id": parse_node_id(node_id),
            "url": parse_node_url(url, allow_loopback=allow_loopback),
            "region": parse_region(region),
            "kind": kind,
            "capabilities": list(dict.fromkeys(capabilities)),
            "priority": priority,
            "enabled": True,
            "secret_hash": secret_hash(secret),
            "created_at": time.time() if now is None else now,
            "rotated_at": None,
        }
        try:
            await self.cloud.nodes.insert_one(doc)
        except DuplicateKeyError:
            raise NodeExists from None
        return doc, secret

    async def list(self) -> list[dict[str, Any]]:
        return await self.cloud.nodes.find().sort([("priority", ASCENDING), ("_id", ASCENDING)]).to_list(None)

    async def rotate(self, node_id: str, *, now: float | None = None, immediate: bool = False) -> str | None:
        """A new secret for the node (None: no such node). The old one stays valid for `ROTATION_GRACE_S`, or stops
        at once with `immediate` (a leaked secret)."""
        now = time.time() if now is None else now
        secret = new_node_secret()
        swap: dict[str, Any] = {"secret_hash": secret_hash(secret), "rotated_at": now}
        if immediate:
            stages: list[dict[str, Any]] = [{"$set": swap}, {"$unset": ["prev_secret_hash", "prev_valid_until"]}]
        else:
            # A pipeline update reads the old hash and writes the new one in a single atomic step.
            stages = [
                {"$set": {"prev_secret_hash": "$secret_hash", "prev_valid_until": now + ROTATION_GRACE_S}},
                {"$set": swap},
            ]
        found = await self.cloud.nodes.update_one({"_id": node_id}, stages)
        return secret if found.matched_count else None

    async def set_enabled(self, node_id: str, enabled: bool) -> bool:
        """False when there is no such node."""
        found = await self.cloud.nodes.update_one({"_id": node_id}, {"$set": {"enabled": enabled}})
        return bool(found.matched_count)

    async def by_secret(self, secret: str, *, now: float | None = None) -> dict[str, Any] | None:
        """The enabled node a secret belongs to: its current secret, or the previous one within the grace period."""
        if not is_node_secret(secret):
            return None
        now = time.time() if now is None else now
        digest = secret_hash(secret)
        return await self.cloud.nodes.find_one(
            {
                "enabled": True,
                "$or": [
                    {"secret_hash": digest},
                    {"prev_secret_hash": digest, "prev_valid_until": {"$gt": now}},
                ],
            }
        )

    async def is_node_url(self, url: str) -> bool:
        """Whether a URL (in audience form) is a registered node's, enabled or not."""
        return await self.cloud.nodes.find_one({"url": url}, projection={"_id": 1}) is not None
