"""wristcall CLI: serve, pair and devices. Runs inside the container (docker exec / compose exec)."""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .config import AppConfig, ConfigError, load_config
from .directory_client import CodeConflict, DirectoryClient, DirectoryError
from .pairing import NotFound, PairingService, format_code
from .providers import ProviderError
from .store import open_database

app = typer.Typer(help="wristcall: voice calls from the Apple Watch to an agent.", no_args_is_help=True)
devices_app = typer.Typer(help="Paired devices and pending requests.", no_args_is_help=True)
app.add_typer(devices_app, name="devices")

DEFAULT_CONFIG = Path("/config/wristcall.yaml")
ConfigOpt = Annotated[
    Path, typer.Option("--config", "-c", envvar="WRISTCALL_CONFIG", help="wristcall.yaml file.")
]


def _load(path: Path) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2) from e


def _pairing(cfg: AppConfig) -> PairingService:
    return PairingService(open_database(cfg.server.data_dir), cfg.server.pairing_approval)


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
        application,
        host=host,
        port=port,
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        proxy_headers=True,
        log_level="info",
    )


@app.command()
def pair(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Generates an 8 digit pairing code (valid for 10 minutes, single use)."""
    cfg = _load(config)
    svc = _pairing(cfg)
    directory = DirectoryClient(cfg.server.directory_url) if cfg.server.directory_url else None
    code = svc.create_code()
    if directory is not None:
        for attempt in range(3):
            try:
                directory.register(cfg.server.public_url, code.code)
                break
            except CodeConflict:
                svc.discard_code(code.code)
                if attempt == 2:
                    typer.echo("error: the directory rejected 3 codes in a row; try again.", err=True)
                    raise typer.Exit(1)
                code = svc.create_code()
            except DirectoryError as e:
                typer.echo(f"warning: could not register with the directory ({e}).", err=True)
                directory = None
                break
    minutes = max(1, round((code.expires_at - time.time()) / 60))
    typer.echo(f"Pairing code: {format_code(code.code)}")
    typer.echo(f"Valid for {minutes} minutes, single use.")
    if directory is not None:
        typer.echo("On the watch: Pair > enter the code.")
    else:
        typer.echo(f"On the watch: Pair > Enter server URL > {cfg.server.public_url} > enter the code.")


@devices_app.command("list")
def devices_list(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Lists paired devices and pending requests."""
    svc = _pairing(_load(config))
    devices = svc.list_devices()
    if not devices:
        typer.echo("No paired devices.")
    for d in devices:
        typer.echo(f"{d.id}  {d.name}  paired on {datetime.fromtimestamp(d.created_at):%Y-%m-%d %H:%M}")
    pending = svc.list_pending()
    if pending:
        typer.echo("Pending requests (approve with: wristcall devices approve <id>):")
        for r in pending:
            typer.echo(f"{r.request_id}  {r.device_name}")


@devices_app.command("approve")
def devices_approve(request_id: str, config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Approves a pairing request (flow B)."""
    svc = _pairing(_load(config))
    try:
        name = svc.approve(request_id)
    except NotFound as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Approved: {name}. The watch receives access in a few seconds.")


@devices_app.command("revoke")
def devices_revoke(device_id: str, config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Revokes a device's access."""
    svc = _pairing(_load(config))
    if not svc.revoke(device_id):
        typer.echo(f"Device not found: {device_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Revoked: {device_id}")
