"""Runs before the server or a CLI command touches the data: imports the 0.2.0 profiles once and
gives owner-less devices to the only user. Safe to run concurrently and on every start.

Warnings about the import (adjusted values, skipped profiles) are logged here, once, by whoever runs it
(the server or a CLI command). A cloud server (epic E9) does not run the bootstrap: it relies on the operator
methods of the storage (users.list, devices.list(None), adopt_orphans; see storage/base.py)."""

import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError

from .agents import DEFAULT_ICON, SLUG, Agent, legacy_spec
from .config import AppConfig
from .storage import Conflict, Storage, User
from .users import new_user_id

log = logging.getLogger("wristcall.bootstrap")

IMPORT_MARK = "profiles_imported_at"
OWNER_HANDLE = "owner"


@dataclass
class BootstrapReport:
    owner_created: str | None = None
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)  # skipped slug → why, never with values
    adjusted: dict[str, list[str]] = field(default_factory=dict)  # imported slug → fields moved into bounds
    profiles_ignored: bool = False
    adopted: int = 0


def profile_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:32].strip("-")
    return slug if SLUG.fullmatch(slug) else "agent"


async def _owner(storage: Storage, now: float, report: BootstrapReport) -> User:
    users = await storage.users.list()
    if users:
        return users[0]
    try:
        user = await storage.users.create(new_user_id(), OWNER_HANDLE, "Owner", now)
        report.owner_created = user.handle
        return user
    except Conflict:  # another process created it first
        user = await storage.users.by_handle(OWNER_HANDLE)
        assert user is not None
        return user


async def import_profiles(storage: Storage, config: AppConfig, now: float, report: BootstrapReport) -> None:
    owner = await _owner(storage, now, report)
    # `default` first: watch 0.1.0 calls the first agent of the list.
    names = sorted(config.profiles, key=lambda n: (n != "default", list(config.profiles).index(n)))
    for name in names:
        profile = config.profiles[name]
        slug = profile_slug(name)
        try:
            spec, adjusted = legacy_spec(profile)
            agent = Agent(
                id=f"ag_{secrets.token_hex(6)}", user_id=owner.id, slug=slug, display_name=profile.display_name[:64] or slug,
                icon=DEFAULT_ICON, call_type="conversation", position=0, spec=spec, created_at=now, updated_at=now,
            )
            await storage.agents.create(agent.to_record())
        except Conflict:
            report.skipped.append(slug)
            report.reasons[slug] = "an agent with that slug exists"
            continue
        except ValidationError as e:
            # Field names only: the values may hold secrets.
            report.skipped.append(slug)
            report.reasons[slug] = "invalid " + ", ".join(sorted({".".join(map(str, x["loc"])) for x in e.errors()}))
            continue
        except ValueError:
            report.skipped.append(slug)
            report.reasons[slug] = "invalid profile"
            continue
        report.imported.append(slug)
        if adjusted:
            report.adjusted[slug] = adjusted
            log.warning("adjusted %s (outside the agent bounds): %s", slug, ", ".join(adjusted))
    for slug in report.skipped:
        log.warning("profile not imported: %s (%s)", slug, report.reasons[slug])
    await storage.meta.set(IMPORT_MARK, str(now))


async def bootstrap(storage: Storage, config: AppConfig, *, now: Callable[[], float] = time.time) -> BootstrapReport:
    report = BootstrapReport()
    if config.profiles:
        if await storage.meta.get(IMPORT_MARK) is None:
            await import_profiles(storage, config, now(), report)
        else:
            report.profiles_ignored = True
    users = await storage.users.list()
    if len(users) == 1:
        report.adopted = await storage.devices.adopt_orphans(users[0].id)
    return report


def log_report(report: BootstrapReport) -> None:
    if report.owner_created:
        log.info("created user '%s' for the imported profiles", report.owner_created)
    if report.imported:
        log.info("imported profiles as agents: %s", ", ".join(report.imported))
    if report.profiles_ignored:
        log.warning("`profiles` in the config is ignored: it was imported already; manage agents with `wristcall agents`")
    if report.adopted:
        log.info("assigned %d device(s) without an owner to the only user", report.adopted)
