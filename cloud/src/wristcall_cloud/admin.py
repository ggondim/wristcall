"""`wristcall-cloud admin`: beta entitlements, invite codes, numbers and the node registry, straight on the database.

One line per item, `key: value`, on stdout; errors go to stderr. Exit codes: 0 done, 1 no such account,
2 bad usage or configuration, 3 the database failed
(only the exception type is printed). The invite codes `invites create` prints are the only time
they exist in clear, and so are the node secrets `nodes add` and `nodes rotate` print: nothing here logs one.
"""

import argparse
import asyncio
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import NoReturn

from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from . import __version__
from .account_store import PLANS, AccountStore
from .cloud_store import CloudModeStore
from .config import ConfigError, admin_settings
from .nodes import CAPABILITIES, KINDS, NodeExists, NodeStore

PROG = "wristcall-cloud admin"
MAX_INVITES = 1000


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _positive(raw: str) -> int:
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(raw)


def _count(raw: str) -> int:
    value = _positive(raw)
    if value > MAX_INVITES:
        raise argparse.ArgumentTypeError(f"must be up to {MAX_INVITES}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    show = commands.add_parser("show", help="what the account has")
    show.add_argument("account")
    grant = commands.add_parser("grant", help="gives the account a beta plan (replacing any)")
    grant.add_argument("account")
    grant.add_argument("--plan", choices=PLANS, required=True)
    grant.add_argument("--days", type=_positive, default=None, help="for how long; without it, with no end")
    revoke = commands.add_parser("revoke", help="takes the account's plan away")
    revoke.add_argument("account")
    invites = commands.add_parser("invites", help="invite codes")
    invites_commands = invites.add_subparsers(dest="invites_command", required=True, parser_class=_Parser)
    create = invites_commands.add_parser("create", help="prints new single-use codes (the only time they show)")
    create.add_argument("--plan", choices=PLANS, required=True)
    create.add_argument("--count", type=_count, default=1)
    create.add_argument("--days", type=_positive, default=30, help="how long the codes can be redeemed")
    commands.add_parser("stats", help="counts")
    nodes = commands.add_parser("nodes", help="wristcall Cloud nodes")
    nodes_commands = nodes.add_subparsers(dest="nodes_command", required=True, parser_class=_Parser)
    add = nodes_commands.add_parser("add", help="registers a node; prints its secret (the only time it shows)")
    add.add_argument("node")
    add.add_argument("--url", required=True)
    add.add_argument("--region", required=True, help="three lowercase letters")
    add.add_argument("--kind", choices=KINDS, required=True)
    add.add_argument("--capability", choices=CAPABILITIES, action="append", default=[], dest="capabilities")
    add.add_argument("--priority", type=int, default=100, help="lower first")
    add.add_argument("--allow-loopback", action="store_true", help="accept a loopback URL (development)")
    nodes_commands.add_parser("list", help="the registered nodes")
    rotate = nodes_commands.add_parser("rotate", help="a new secret; the old one works for 15 more minutes")
    rotate.add_argument("node")
    rotate.add_argument("--now", action="store_true", help="the old secret stops working at once (a leak)")
    for name in ("enable", "disable"):
        nodes_commands.add_parser(name, help=f"{name}s a node").add_argument("node")
    return parser


def _iso(seconds: float | None) -> str:
    return "never" if seconds is None else datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def _line(key: str, value: object) -> None:
    print(f"{key}: {value}")


def _no_such_account() -> int:
    print(f"{PROG}: no such account", file=sys.stderr)
    return 1


def _no_such_node() -> int:
    print(f"{PROG}: no such node", file=sys.stderr)
    return 1


async def _run_nodes(args: argparse.Namespace, nodes: NodeStore) -> int:
    if args.nodes_command == "add":
        try:
            doc, secret = await nodes.add(
                args.node,
                url=args.url,
                region=args.region,
                kind=args.kind,
                capabilities=args.capabilities,
                priority=args.priority,
                allow_loopback=args.allow_loopback,
            )
        except ValueError as e:  # never carries the URL
            print(f"{PROG}: {e}", file=sys.stderr)
            return 2
        except NodeExists:
            print(f"{PROG}: node already exists", file=sys.stderr)
            return 2
        _line("node", doc["_id"])
        _line("url", doc["url"])
        _line("secret", secret)
        return 0
    if args.nodes_command == "list":
        for node in await nodes.list():
            enabled = "true" if node["enabled"] else "false"
            _line(
                "node",
                f"{node['_id']} url={node['url']} region={node['region']} kind={node['kind']} "
                f"enabled={enabled} last_seen_at={_iso(node.get('last_seen_at'))}",
            )
        return 0
    if args.nodes_command == "rotate":
        secret = await nodes.rotate(args.node, immediate=args.now)
        if secret is None:
            return _no_such_node()
        _line("secret", secret)
        return 0
    enabled = args.nodes_command == "enable"
    if not await nodes.set_enabled(args.node, enabled):
        return _no_such_node()
    _line("enabled", "true" if enabled else "false")
    return 0


async def run(args: argparse.Namespace, store: AccountStore) -> int:
    now = time.time()
    if args.command == "nodes":
        return await _run_nodes(args, NodeStore(CloudModeStore(store.db)))
    if args.command == "stats":
        entitled = {"$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}]}
        _line("accounts", await store.accounts.count_documents({}))
        _line("anonymous_24h", await store.count_created_since(now - 86400))
        _line("devices", await store.devices.count_documents({}))
        _line("identities", await store.identities.count_documents({}))
        _line("entitlements", await store.entitlements.count_documents(entitled))
        return 0
    if args.command == "invites":
        for code in await store.create_invites(args.plan, args.count, days=args.days, now=now):
            _line("code", code)
        return 0
    account = await store.get_account(args.account)
    if account is None:
        return _no_such_account()
    if args.command == "show":
        entitlement = await store.entitlement(args.account, now=now)
        identities = await store.identities_of(args.account)
        _line("account", args.account)
        _line("kind", account.get("kind", "legacy"))
        _line("created_at", _iso(account.get("created_at")))
        _line("last_seen_at", _iso(account.get("last_seen_at")))
        _line("devices", len(await store.list_devices(args.account)))
        _line("identities", ",".join(i["kind"] for i in identities) or "none")
        _line("plan", "none" if entitlement is None else entitlement["plan"])
        if entitlement is not None:
            _line("plan_expires_at", _iso(entitlement.get("expires_at")))
        return 0
    if args.command == "grant":
        granted = await store.grant(args.account, args.plan, days=args.days, now=now)
        _line("account", args.account)
        _line("plan", granted["plan"])
        _line("expires_at", _iso(granted["expires_at"]))
        return 0
    _line("revoked", "true" if await store.revoke_entitlement(args.account) else "false")
    return 0


async def _run_on_database(args: argparse.Namespace, url: str, database: str) -> int:
    client: AsyncMongoClient = AsyncMongoClient(
        url, appname=f"wristcall-cloud-admin/{__version__}", tz_aware=True, serverSelectionTimeoutMS=10000
    )
    try:
        return await run(args, AccountStore(client[database]))
    finally:
        await client.close()


def main(argv: list[str], env: Mapping[str, str]) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # usage error (2) or --help (0)
        return e.code if isinstance(e.code, int) else 2
    try:
        url, database = admin_settings(env)
    except ConfigError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run_on_database(args, url, database))
    except PyMongoError as e:  # the message of a driver error can name hosts; the type is enough
        print(f"{PROG}: database error ({type(e).__name__})", file=sys.stderr)
        return 3
