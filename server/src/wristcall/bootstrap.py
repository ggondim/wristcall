"""Runs before the server or a CLI command touches the data: imports the 0.2.0 profiles once and
gives owner-less devices to the only user. Safe to run concurrently and on every start."""

import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .agents import DEFAULT_ICON, SLUG, Agent, spec_from_profile
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
    profiles_ignored: bool = False
    adopted: int = 0


def profile_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:32].strip("-")
    return slug if SLUG.match(slug) else "agent"


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
        agent = Agent(
            id=f"ag_{secrets.token_hex(6)}", user_id=owner.id, slug=slug, display_name=profile.display_name[:64] or slug,
            icon=DEFAULT_ICON, call_type="conversation", position=0, spec=spec_from_profile(profile),
            created_at=now, updated_at=now,
        )
        try:
            await storage.agents.create(agent.to_record())
            report.imported.append(slug)
        except Conflict:
            report.skipped.append(slug)
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
    if report.skipped:
        log.warning("profiles not imported (an agent with that slug exists): %s", ", ".join(report.skipped))
    if report.profiles_ignored:
        log.warning("`profiles` in the config is ignored: it was imported already; manage agents with `wristcall agents`")
    if report.adopted:
        log.info("assigned %d device(s) without an owner to the only user", report.adopted)
