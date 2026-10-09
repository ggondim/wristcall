"""wristcall CLI: serve, users, devices, agents. Runs inside the container (docker exec / compose exec)."""

import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar

import httpx
import typer
import uvicorn

from .agents import Agent, AgentError, AgentService, agent_detail
from .bootstrap import bootstrap
from .config import AppConfig, ConfigError, load_config
from .directory_client import DirectoryClient, DirectoryError
from .pairing import DeviceLimit, NotFound, PairingService, format_code, issue_code
from .providers import ProviderError
from .storage import Storage, open_sqlite_storage
from .users import UserError, UserService

app = typer.Typer(help="wristcall: voice calls from the Apple Watch to an agent.", no_args_is_help=True)
devices_app = typer.Typer(help="Paired devices and pending requests.", no_args_is_help=True)
users_app = typer.Typer(help="Users of this server.", no_args_is_help=True)
tokens_app = typer.Typer(help="A user's API tokens (for the management API).", no_args_is_help=True)
agents_app = typer.Typer(help="A user's agents.", no_args_is_help=True)
app.add_typer(devices_app, name="devices")
app.add_typer(users_app, name="users")
users_app.add_typer(tokens_app, name="tokens")
app.add_typer(agents_app, name="agents")

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


# ---------- users ----------


@users_app.command("list")
def users_list(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Lists users with their number of agents and devices."""

    async def body(ctx: Ctx):
        return [
            (u, await ctx.storage.agents.count(u.id), await ctx.storage.devices.count(u.id))
            for u in await ctx.storage.users.list()
        ]

    rows = _run(config, body)
    if not rows:
        typer.echo("No users. Create one with: wristcall users add <handle>")
    for u, agents, devices in rows:
        typer.echo(f"{u.handle}  {u.display_name}  {agents} agent(s)  {devices} device(s)  since {_when(u.created_at)}")


@users_app.command("add")
def users_add(
    handle: str,
    config: ConfigOpt = DEFAULT_CONFIG,
    name: Annotated[str | None, typer.Option("--name", help="Display name.")] = None,
) -> None:
    """Creates a user (no agents yet: add them with `wristcall agents add --user <handle>`)."""
    u = _run(config, lambda ctx: ctx.users.create(handle, name))
    typer.echo(f"Created user {u.handle}.")


@users_app.command("edit")
def users_edit(
    handle: str,
    config: ConfigOpt = DEFAULT_CONFIG,
    new_handle: Annotated[str | None, typer.Option("--handle", help="New handle.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="New display name.")] = None,
) -> None:
    """Renames a user (for example the `owner` created by the profile import)."""
    u = _run(config, lambda ctx: ctx.users.rename(handle, new_handle, name))
    typer.echo(f"User {u.handle}: {u.display_name}")


@users_app.command("rm")
def users_rm(
    handle: str, config: ConfigOpt = DEFAULT_CONFIG, yes: Annotated[bool, typer.Option("--yes", "-y")] = False
) -> None:
    """Deletes a user with their agents, devices and tokens."""
    _confirm(yes, f"Delete user {handle} with all agents, devices and tokens?")
    u = _run(config, lambda ctx: ctx.users.delete(handle))
    typer.echo(f"Deleted user {u.handle}.")


@tokens_app.command("add")
def tokens_add(
    config: ConfigOpt = DEFAULT_CONFIG,
    user: UserOpt = None,
    name: Annotated[str, typer.Option("--name", help="What this token is for.")] = "cli",
) -> None:
    """Creates an API token. It is printed once and cannot be shown again."""

    async def body(ctx: Ctx):
        owner = await ctx.users.resolve(user)
        return owner, await ctx.users.issue_token(owner.id, name)

    owner, (record, token) = _run(config, body)
    typer.echo(f"API token {record.id} for {owner.handle} ({record.name}). Store it now; it is not shown again:")
    typer.echo(token)


@tokens_app.command("list")
def tokens_list(config: ConfigOpt = DEFAULT_CONFIG, user: UserOpt = None) -> None:
    """Lists a user's active API tokens (never the tokens themselves)."""

    async def body(ctx: Ctx):
        return await ctx.users.list_tokens((await ctx.users.resolve(user)).id)

    tokens = _run(config, body)
    if not tokens:
        typer.echo("No API tokens.")
    for t in tokens:
        used = _when(t.last_used_at) if t.last_used_at else "never"
        typer.echo(f"{t.id}  {t.name}  created {_when(t.created_at)}  last used {used}")


@tokens_app.command("revoke")
def tokens_revoke(token_id: str, config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Revokes an API token."""
    if not _run(config, lambda ctx: ctx.users.revoke_token(token_id)):
        typer.echo(f"no active token with id {token_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Revoked: {token_id}")


# ---------- agents ----------


def _endpoint(value: str) -> dict[str, Any]:
    """A provider name from the YAML, or a JSON object for the user's own URL."""
    if value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            raise AgentError("invalid", f"invalid JSON endpoint: {e}") from None
        if not isinstance(parsed, dict):
            raise AgentError("invalid", "an endpoint must be a JSON object")
        return parsed
    return {"provider": value}


def _read_text(path: Path) -> str:
    """Reads a UTF-8 file (or stdin for `-`), turning I/O and decoding failures into AgentError."""
    try:
        return sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise AgentError("invalid", f"{path} is not UTF-8 text") from None
    except OSError as e:
        raise AgentError("invalid", f"cannot read {path}: {e.strerror or e}") from None


def _agent_input(
    from_json: Path | None,
    name: str | None,
    icon: str | None,
    language: str | None,
    turn_end: str | None,
    silence_ms: int | None,
    call_type: str | None,
    position: int | None,
    stt: str | None,
    action: str | None,
    tts: str | None,
    prompt: str | None,
    prompt_file: Path | None,
    fallback: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if from_json is not None:
        raw = _read_text(from_json)
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AgentError("invalid", f"invalid JSON in {from_json}: {e}") from None
        if not isinstance(loaded, dict):
            raise AgentError("invalid", f"{from_json} must hold a JSON object")
        data.update(loaded)
        for key in ("id", "created_at", "updated_at"):  # `agents show` output fed back
            data.pop(key, None)
    flags: dict[str, Any] = {
        "display_name": name, "icon": icon, "language": language, "turn_end": turn_end, "call_type": call_type,
        "position": position, "fallback_message": fallback,
    }
    data.update({k: v for k, v in flags.items() if v is not None})
    for field, value in (("stt", stt), ("action", action), ("tts", tts)):
        if value is not None:
            data[field] = _endpoint(value)
    if prompt is not None:
        data["system_prompt"] = prompt
    if prompt_file is not None:
        data["system_prompt"] = _read_text(prompt_file)
    if silence_ms is not None:
        data["vad"] = {**(data.get("vad") or {}), "silence_ms": silence_ms}
    return data


NameOpt = Annotated[str | None, typer.Option("--name", help="Display name on the watch.")]
IconOpt = Annotated[str | None, typer.Option("--icon", help="SF Symbol name, e.g. waveform, figure.run.")]
LanguageOpt = Annotated[str | None, typer.Option("--language", help="Language code for STT, e.g. pt, en.")]
TurnEndOpt = Annotated[str | None, typer.Option("--turn-end", help="auto (silence ends the turn) or manual (mute ends it).")]
SilenceOpt = Annotated[int | None, typer.Option("--silence-ms", help="Silence that ends a turn in auto mode.")]
CallTypeOpt = Annotated[str | None, typer.Option("--call-type", help="conversation (one-shot and monologue: later).")]
PositionOpt = Annotated[int | None, typer.Option("--position", help="Order on the watch; 0 comes first.")]
SttOpt = Annotated[str | None, typer.Option("--stt", help="Provider name, or a JSON object with your own URL.")]
ActionOpt = Annotated[str | None, typer.Option("--action", help="Provider name, or a JSON object with your own URL.")]
TtsOpt = Annotated[str | None, typer.Option("--tts", help="Provider name, or a JSON object with your own URL.")]
PromptOpt = Annotated[str | None, typer.Option("--prompt", help="System prompt.")]
PromptFileOpt = Annotated[Path | None, typer.Option("--prompt-file", help="Read the system prompt from a file.")]
FallbackOpt = Annotated[str | None, typer.Option("--fallback", help="Spoken when the agent fails.")]
JsonOpt = Annotated[Path | None, typer.Option("--from-json", help="JSON file with the fields (- for stdin); flags win.")]


def _print_agent(agent: Agent) -> None:
    typer.echo(json.dumps(agent_detail(agent), indent=2, ensure_ascii=False))


@agents_app.command("list")
def agents_list(config: ConfigOpt = DEFAULT_CONFIG, user: UserOpt = None) -> None:
    """Lists a user's agents in the order the watch shows them."""

    async def body(ctx: Ctx):
        return await ctx.agents.list((await ctx.users.resolve(user)).id)

    agents = _run(config, body)
    if not agents:
        typer.echo("No agents. Add one with: wristcall agents add <slug>")
    for a in agents:
        s = a.spec
        stages = " ".join(
            f"{f}={e.provider if hasattr(e, 'provider') else 'custom'}" for f, e in (("stt", s.stt), ("action", s.action), ("tts", s.tts))
        )
        typer.echo(f"{a.position}  {a.slug}  {a.display_name}  {a.icon}  {a.call_type}  turn_end={s.turn_end}  {stages}  {a.id}")


@agents_app.command("show")
def agents_show(ref: str, config: ConfigOpt = DEFAULT_CONFIG, user: UserOpt = None) -> None:
    """Shows an agent as JSON (secrets as ***); `--from-json` accepts the same shape."""

    async def body(ctx: Ctx):
        return await ctx.agents.get((await ctx.users.resolve(user)).id, ref)

    _print_agent(_run(config, body))


@agents_app.command("add")
def agents_add(
    slug: str,
    config: ConfigOpt = DEFAULT_CONFIG,
    user: UserOpt = None,
    name: NameOpt = None,
    icon: IconOpt = None,
    language: LanguageOpt = None,
    turn_end: TurnEndOpt = None,
    silence_ms: SilenceOpt = None,
    call_type: CallTypeOpt = None,
    position: PositionOpt = None,
    stt: SttOpt = None,
    action: ActionOpt = None,
    tts: TtsOpt = None,
    prompt: PromptOpt = None,
    prompt_file: PromptFileOpt = None,
    fallback: FallbackOpt = None,
    from_json: JsonOpt = None,
) -> None:
    """Adds an agent. Without --stt/--action/--tts, uses the server's only provider of each kind."""

    async def body(ctx: Ctx):
        data = _agent_input(from_json, name, icon, language, turn_end, silence_ms, call_type, position, stt, action, tts, prompt, prompt_file, fallback)
        data["slug"] = slug
        return await ctx.agents.create((await ctx.users.resolve(user)).id, data)

    agent = _run(config, body)
    typer.echo(f"Added agent {agent.slug} ({agent.id}).")


@agents_app.command("edit")
def agents_edit(
    ref: str,
    config: ConfigOpt = DEFAULT_CONFIG,
    user: UserOpt = None,
    slug: Annotated[str | None, typer.Option("--slug", help="New slug.")] = None,
    name: NameOpt = None,
    icon: IconOpt = None,
    language: LanguageOpt = None,
    turn_end: TurnEndOpt = None,
    silence_ms: SilenceOpt = None,
    call_type: CallTypeOpt = None,
    position: PositionOpt = None,
    stt: SttOpt = None,
    action: ActionOpt = None,
    tts: TtsOpt = None,
    prompt: PromptOpt = None,
    prompt_file: PromptFileOpt = None,
    fallback: FallbackOpt = None,
    from_json: JsonOpt = None,
) -> None:
    """Changes only the given fields of an agent."""

    async def body(ctx: Ctx):
        data = _agent_input(from_json, name, icon, language, turn_end, silence_ms, call_type, position, stt, action, tts, prompt, prompt_file, fallback)
        if slug is not None:
            data["slug"] = slug
        return await ctx.agents.update((await ctx.users.resolve(user)).id, ref, data)

    agent = _run(config, body)
    typer.echo(f"Updated agent {agent.slug} ({agent.id}).")


@agents_app.command("rm")
def agents_rm(
    ref: str, config: ConfigOpt = DEFAULT_CONFIG, user: UserOpt = None, yes: Annotated[bool, typer.Option("--yes", "-y")] = False
) -> None:
    """Deletes an agent."""
    _confirm(yes, f"Delete agent {ref}?")

    async def body(ctx: Ctx):
        await ctx.agents.delete((await ctx.users.resolve(user)).id, ref)

    _run(config, body)
    typer.echo(f"Deleted agent {ref}.")
