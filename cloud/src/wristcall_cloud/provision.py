"""What an account gets with a plan: the managed Cloud entry in its agenda and, on `full`, the "Voice notes" agent.

The Cloud entry is the `servers` document with `kind: "cloud"` and `managed: true`: only the Cloud creates it, one per
account. Its agents are not copied into it: they are the account's `cloud_agents`, read when the entry is shown.
"Voice notes" is created once per account (the entry's `voice_notes_at` says it was done): an account that deletes it
does not get it back on the next grant. Nothing here logs a URL or anything the user wrote.
"""

import logging
import secrets
import time
from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .cloud_store import CloudModeStore
from .config import ProvisionSettings
from .node_core import list_agents
from .store import Store, public_server

log = logging.getLogger("wristcall_cloud.provision")

CLOUD_ENTRY_NAME = "Wristcall Cloud"
VOICE_NOTES_SLUG = "voice-notes"
LUSOPHONE = frozenset({"BR", "PT", "AO", "MZ", "CV", "GW", "ST", "TL"})

_AGENDA_FIELDS = ("id", "slug", "display_name", "icon", "call_type")


def voice_notes_language(country: str | None, default: str) -> str:
    """`pt` for a Portuguese-speaking country, `default` otherwise (also without a country)."""
    return "pt" if country in LUSOPHONE else default


def is_managed(doc: dict[str, Any]) -> bool:
    """Whether a `servers` document is the account's Cloud entry (older `kind: cloud` entries apps made are not)."""
    return doc.get("kind") == "cloud" and doc.get("managed") is True


def _entry_filter(account: str) -> dict[str, Any]:
    return {"account": account, "kind": "cloud", "managed": True}


async def cloud_entry(store: Store, account: str) -> dict[str, Any] | None:
    """The account's Cloud entry, None when it has none."""
    return await store.servers.find_one(_entry_filter(account))


def agenda_agent(agent: dict[str, Any]) -> dict[str, Any]:
    """An agent in its public form (`public_agent`) as a server's agenda lists it: no spec, `color` only when set."""
    shown = {k: agent[k] for k in _AGENDA_FIELDS}
    if agent.get("color") is not None:
        shown["color"] = agent["color"]
    return shown


async def cloud_entry_view(cloud: CloudModeStore, doc: dict[str, Any]) -> dict[str, Any]:
    """The Cloud entry as the API shows a server, its agents read from the account's `cloud_agents`."""
    return public_server(doc) | {"agents": [agenda_agent(a) for a in await list_agents(cloud, doc["account"])]}


async def _upsert_entry(
    store: Store, account: str, settings: ProvisionSettings, now: float
) -> tuple[dict[str, Any] | None, bool]:
    """The account's Cloud entry and whether this call created it; (None, False) when the account already has a
    server of its own at the Cloud's URL (that server is never turned into a managed one)."""
    new_id = f"srv_{secrets.token_hex(6)}"
    try:
        doc = await store.servers.find_one_and_update(
            _entry_filter(account),
            {
                "$setOnInsert": {
                    "_id": new_id,
                    "url": settings.cloud_url,
                    "name": CLOUD_ENTRY_NAME,
                    "linked": True,
                    "agents": [],
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        # Either a concurrent provisioning inserted the entry first, or the URL is the account's own server.
        doc = await cloud_entry(store, account)
        if doc is None:
            log.warning("cloud entry not created: the account has a server of its own at the Cloud's URL")
            return None, False
        return doc, False
    return doc, doc["_id"] == new_id


async def _add_voice_notes(cloud: CloudModeStore, account: str, language: str, stt: str, now: float) -> bool:
    """Inserts "Voice notes" unless the account has an agent with its slug; whether this call inserted it."""
    if await cloud.agents.find_one({"account": account, "slug": VOICE_NOTES_SLUG}, {"_id": 1}) is not None:
        return False
    last = await cloud.agents.find_one({"account": account}, {"position": 1}, sort=[("position", DESCENDING)])
    doc = {
        "_id": f"ag_{secrets.token_hex(6)}",
        "account": account,
        "slug": VOICE_NOTES_SLUG,
        "display_name": "Voice notes",
        "icon": "note.text",
        "color": "yellow",
        "call_type": "one-shot",
        "spec": {"language": language, "stt": {"provider": stt}, "action": None, "retention_days": None},
        "position": 0 if last is None else (last.get("position") or 0) + 1,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await cloud.agents.insert_one(doc)
    except DuplicateKeyError:  # the unique `account_slug` index: a concurrent provisioning (or the user) won
        return False
    return True


async def provision(
    store: Store,
    cloud: CloudModeStore,
    account: str,
    plan: str,
    settings: ProvisionSettings,
    *,
    language: str | None = None,
    now: float | None = None,
) -> dict[str, bool]:
    """Makes sure the account with `plan` has its Cloud entry and, on `full`, that "Voice notes" was created once
    (in `language`, default `settings.language`). Idempotent; returns what this call created."""
    now = time.time() if now is None else now
    entry, entry_created = await _upsert_entry(store, account, settings, now)
    if entry is None:
        return {"entry": False, "voice_notes": False}
    if entry_created:
        log.info("cloud entry created")
    if plan != "full" or "voice_notes_at" in entry:
        return {"entry": entry_created, "voice_notes": False}
    language = settings.language if language is None else language
    created = await _add_voice_notes(cloud, account, language, settings.stt_provider, now)
    await store.servers.update_one(
        {"_id": entry["_id"], "voice_notes_at": {"$exists": False}}, {"$set": {"voice_notes_at": now}}
    )
    if created:
        log.info("voice notes agent created")
    return {"entry": entry_created, "voice_notes": created}
