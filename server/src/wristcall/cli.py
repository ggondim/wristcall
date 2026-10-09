"""wristcall CLI: serve, pair and devices. Runs inside the container (docker exec / compose exec)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, TypeVar

import httpx
import typer
import uvicorn

from .agents import AgentError, AgentService
from .bootstrap import bootstrap
from .config import AppConfig, ConfigError, load_config
from .directory_client import DirectoryClient, DirectoryError
from .pairing import DeviceLimit, NotFound, PairingService, format_code, issue_code
from .providers import ProviderError
from .storage import Storage, open_sqlite_storage
from .users import UserError, UserService

app = typer.Typer(help="wristcall: voice calls from the Apple Watch to an agent.", no_args_is_help=True)
devices_app = typer.Typer(help="Paired devices and pending requests.", no_args_is_help=True)
app.add_typer(devices_app, name="devices")

DEFAULT_CONFIG = Path("/config/wristcall.yaml")
ConfigOpt = Annotated[
    Path, typer.Option("--config", "-c", envvar="WRISTCALL_CONFIG", help="wristcall.yaml file.")
]
UserOpt = Annotated[
    str | None, typer.Option("--user", "-u", help="User handle. Optional when the server has a single user.")
]
T = TypeVar("T")


def _load(path: Path) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2) from e


@dataclass
class Ctx:
    cfg: AppConfig
    storage: Storage
    users: UserService
    pairing: PairingService
    agents: AgentService


def _run(config: Path, body: Callable[[Ctx], Awaitable[T]]) -> T:
    """Opens the database (migrating and importing profiles if needed), runs body, closes. Domain errors exit 1."""
    cfg = _load(config)

    async def main() -> T:
        storage = open_sqlite_storage(cfg.server.data_dir)
        try:
            await bootstrap(storage, cfg)
            async with httpx.AsyncClient() as http:
                ctx = Ctx(
                    cfg, storage, UserService(storage),
                    PairingService(storage, cfg.server.pairing_approval, max_devices_per_user=cfg.limits.max_devices_per_user),
                    AgentService(storage, cfg, http),
                )
                return await body(ctx)
        finally:
            await storage.close()

    try:
        return asyncio.run(main())
    except (UserError, AgentError, NotFound, DeviceLimit, DirectoryError) as e:
        typer.echo(f"error: {getattr(e, 'message', None) or e}", err=True)
        raise typer.Exit(1) from None


def _when(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}"


def _confirm(yes: bool, question: str) -> None:
    if not yes and not typer.confirm(question):
        raise typer.Exit(1)


@app.command()
def serve(config: ConfigOpt = DEFAULT_CONFIG, host: str = "0.0.0.0", port: int = 8080) -> None:
    """Starts the HTTP/WebSocket server."""
    from .app import create_app

    cfg = _load(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        application = create_app(cfg)
    except ProviderError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2) from e
    # Trusted proxy: FORWARDED_ALLOW_IPS variable; behind Cloudflare, use server.client_ip_header.
    uvicorn.run(
        application, host=host, port=port, ws_ping_interval=20.0, ws_ping_timeout=20.0, proxy_headers=True, log_level="info"
    )


@app.command()
def pair(config: ConfigOpt = DEFAULT_CONFIG, user: UserOpt = None) -> None:
    """Generates an 8 digit pairing code for a user (valid 10 minutes, single use)."""

    async def body(ctx: Ctx):
        owner = await ctx.users.resolve(user)
        directory = DirectoryClient(ctx.cfg.server.directory_url) if ctx.cfg.server.directory_url else None
        return owner, await issue_code(ctx.pairing, owner.id, ctx.cfg.server.public_url, directory)

    owner, issued = _run(config, body)
    if issued.warning:
        typer.echo(f"warning: {issued.warning}.", err=True)
    minutes = max(1, round((issued.code.expires_at - time.time()) / 60))
    # "Pairing code:" starts the line: the watch integration tests (TestServer.swift) parse it.
    typer.echo(f"Pairing code: {format_code(issued.code.code)}")
    typer.echo(f"For user {owner.handle}. Valid for {minutes} minutes, single use.")
    if issued.via_directory:
        typer.echo("On the watch: Pair > enter the code.")
    else:
        typer.echo(f"On the watch: Pair > Enter server URL > {_load(config).server.public_url} > enter the code.")


# ---------- devices ----------


@devices_app.command("list")
def devices_list(config: ConfigOpt = DEFAULT_CONFIG, user: UserOpt = None) -> None:
    """Lists paired devices (all, or one user's) and pending requests."""

    async def body(ctx: Ctx):
        owner = await ctx.users.resolve(user) if user else None
        handles = {u.id: u.handle for u in await ctx.storage.users.list()}
        return handles, await ctx.pairing.list_devices(owner.id if owner else None), await ctx.pairing.list_pending()

    handles, devices, pending = _run(config, body)
    if not devices:
        typer.echo("No paired devices.")
    for d in devices:
        typer.echo(f"{d.id}  {d.name}  {handles.get(d.user_id or '', '-')}  paired on {_when(d.created_at)}")
    if pending:
        typer.echo("Pending requests (approve with: wristcall devices approve <id>):")
        for r in pending:
            typer.echo(f"{r.request_id}  {r.device_name}")


@devices_app.command("approve")
def devices_approve(request_id: str, config: ConfigOpt = DEFAULT_CONFIG, user: UserOpt = None) -> None:
    """Approves a pairing request (flow B) for a user."""

    async def body(ctx: Ctx):
        owner = await ctx.users.resolve(user)
        return owner, await ctx.pairing.approve(request_id, owner.id)

    owner, name = _run(config, body)
    typer.echo(f"Approved: {name} ({owner.handle}). The watch receives access in a few seconds.")


@devices_app.command("revoke")
def devices_revoke(device_id: str, config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Revokes a device: its token stops working right away."""
    if not _run(config, lambda ctx: ctx.pairing.revoke(device_id)):
        typer.echo(f"no active device with id {device_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Revoked: {device_id}")
