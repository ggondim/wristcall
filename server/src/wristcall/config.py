"""Operator configuration: YAML with ${ENV} references. Server, providers on offer and limits.

`profiles` (inheriting from `default`) is the 0.2.0 format: imported once as agents (see bootstrap.py).
"""

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .audience import AudienceError, is_loopback, normalize_audience
from .history_codec import HistoryKeyError, parse_key


class ConfigError(Exception):
    pass


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NESTED = ("vad", "timeouts")


def _interpolate(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in env:
                raise ConfigError(f"missing environment variable: {name}")
            return env[name]

        return _ENV_REF.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, env) for v in value]
    return value


class WarmupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_start: bool = False
    on_call: bool = False
    every_s: float = 0.0
    text: str = "Hello."
    language: str = "en"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    warmup: WarmupConfig | None = None

    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class VadConfig(BaseModel):
    # Bounds: agents come from users through the API; max_turn_ms also caps the audio a turn keeps in memory.
    model_config = ConfigDict(extra="forbid")
    type: Literal["silero", "energy"] = "silero"
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    energy_dbfs: float = Field(default=-45.0, ge=-120.0, le=0.0)
    silence_ms: int = Field(default=800, ge=100, le=10_000)
    min_speech_ms: int = Field(default=300, ge=0, le=5_000)
    max_turn_ms: int = Field(default=60_000, ge=1_000, le=300_000)
    pre_roll_ms: int = Field(default=300, ge=0, le=2_000)


class Timeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stt_s: float = Field(default=10.0, gt=0, le=120)
    first_token_s: float = Field(default=15.0, gt=0, le=120)
    tts_s: float = Field(default=15.0, gt=0, le=120)


class LegacyVadConfig(BaseModel):
    """VadConfig of the 0.2.0 profiles: same fields, no bounds (values are adjusted on import, see bootstrap.py)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["silero", "energy"] = "silero"
    threshold: float = 0.5
    energy_dbfs: float = -45.0
    silence_ms: int = 800
    min_speech_ms: int = 300
    max_turn_ms: int = 60_000
    pre_roll_ms: int = 300


class LegacyTimeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stt_s: float = 10.0
    first_token_s: float = 15.0
    tts_s: float = 15.0


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    language: str = "en"
    stt: str
    responder: str
    tts: str
    system_prompt: str = ""
    fallback_message: str = "Sorry, I couldn't answer right now."
    vad: LegacyVadConfig = Field(default_factory=LegacyVadConfig)
    timeouts: LegacyTimeouts = Field(default_factory=LegacyTimeouts)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _cors_origin(raw: str) -> str:
    """An exact origin (`scheme://host[:port]`) in lowercase: https, or http only on loopback. Never `*`.

    The error does not repeat the value.
    """
    msg = "each origin must be scheme://host[:port] with no path (https; http only for localhost)"
    try:
        parts = urlsplit(raw)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError(msg) from None
    valid = (
        parts.scheme in ("http", "https")
        and host
        and not parts.path
        and not parts.username
        and not parts.password
        and not any(c in raw for c in "*?#@\\ ")
        and (parts.scheme == "https" or host in _LOOPBACK_HOSTS)
    )
    if not valid:
        raise ValueError(msg)
    shown = f"[{host}]" if ":" in host else host
    default_port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{shown}" + (f":{port}" if port not in (None, default_port) else "")


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # self-hosted: users, devices, agents and history live in this server's SQLite. cloud: the same binary runs as a
    # node of the Wristcall Cloud, which keeps all of that (see CloudNodeConfig); nothing is written to disk.
    mode: Literal["self-hosted", "cloud"] = "self-hosted"
    public_url: str
    directory_url: str | None = None
    pairing_approval: Literal["code", "manual"] = "code"
    data_dir: Path = Path("/data")
    client_ip_header: str | None = None
    # Browser origins allowed to call the management API (the wristcall PWA); empty: no CORS headers at all.
    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("cors_origins")
    @classmethod
    def _check_cors_origins(cls, value: list[str]) -> list[str]:
        origins: list[str] = []
        for raw in value:
            origin = _cors_origin(raw.strip())
            if origin not in origins:
                origins.append(origin)
        return origins


class ConcurrentCallsConfig(BaseModel):
    """Most calls open at once, per kind; null: no limit. A call that finds its kind full gets `busy`."""

    model_config = ConfigDict(extra="forbid")
    conversation: int | None = Field(default=None, ge=1)
    # One-shot and monologue: a call counts until it is transcribed and delivered, after the hang-up.
    one_way: int | None = Field(default=None, ge=1)


# A cloud-mode node's limits when the operator does not set them, measured on the GPU of the `home` node (the XTTS
# queue is the bottleneck); self-hosted servers have none.
CLOUD_NODE_CONCURRENT_CALLS: dict[str, int] = {"conversation": 2, "one_way": 2}


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_agents_per_user: int = Field(default=20, ge=1)
    max_devices_per_user: int = Field(default=10, ge=1)
    # Agents may point at the user's own STT/action/TTS URLs. Turn off on a server with untrusted users:
    # the server would make requests to any URL they give, including the internal network.
    custom_endpoints: bool = True
    # With custom_endpoints on: only https URLs whose host resolves to public addresses only (no localhost, private
    # ranges or internal names). Checked when an agent is saved, when a call starts and on every connection.
    public_only: bool = False
    # One-way agents (one-shot, monologue) may have no action: the transcript is only kept in the history.
    history_only_one_way: bool = False
    # Provider types a custom endpoint may use. The fake types (tests) can be made to allocate huge buffers.
    custom_endpoint_types: list[str] = Field(
        default_factory=lambda: ["openai_stt", "openai_chat", "openai_tts", "webhook"]
    )
    # Longest one-way call (one-shot or monologue): the server stops recording and delivers what it got.
    max_one_way_call_s: int = Field(default=1800, ge=60, le=14_400)
    # Longest conversation, from session.start; null: no limit. A cloud-mode node defaults it to 3600.
    max_conversation_s: int | None = Field(default=None, ge=60, le=14_400)
    # Protects the GPU: calls beyond these get `busy` at session.start.
    max_concurrent_calls: ConcurrentCallsConfig = Field(default_factory=ConcurrentCallsConfig)


class HistoryConfig(BaseModel):
    """Call history (design decision 16): how long it stays and whether its text is encrypted at rest."""

    model_config = ConfigDict(extra="forbid")
    # Days a call stays when its agent does not choose; null keeps it until the user deletes it.
    default_retention_days: int | None = Field(default=None, ge=1, le=36_500)
    # Ceiling for every agent, "forever" included; null: no ceiling.
    max_retention_days: int | None = Field(default=None, ge=1, le=36_500)
    # 32 random bytes in base64 (`wristcall history new-key`). Losing it loses the encrypted history.
    encryption_key: str | None = None
    # How often the server deletes expired calls (also once at startup).
    purge_every_s: int = Field(default=3600, ge=60, le=86_400)

    @field_validator("encryption_key")
    @classmethod
    def _check_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parse_key(value)
        except HistoryKeyError as e:
            raise ValueError(str(e)) from None
        return value

    @model_validator(mode="after")
    def _check_ceiling(self) -> "HistoryConfig":
        if self.max_retention_days is not None and (
            self.default_retention_days is None or self.default_retention_days > self.max_retention_days
        ):
            raise ValueError("default_retention_days must be set and at most max_retention_days")
        return self

    def key(self) -> bytes | None:
        return parse_key(self.encryption_key) if self.encryption_key else None

    def effective_days(self, agent_days: "int | Literal['forever'] | None") -> int | None:
        """The agent's choice (None = the operator's default), within the ceiling. None: kept until deleted."""
        days = self.default_retention_days if agent_days is None else None if agent_days == "forever" else agent_days
        if self.max_retention_days is None:
            return days
        return self.max_retention_days if days is None else min(days, self.max_retention_days)


_LOCAL_HOSTS = ("localhost", "127.0.0.1")
ClientId = Annotated[str, StringConstraints(pattern=r"^\S{1,255}$")]


def _check_base_url(value: str, what: str) -> str:
    """An https URL (http only for localhost/127.0.0.1) without query, fragment or credentials; no trailing slash."""
    value = value.strip()
    url = urlsplit(value)
    if url.query or url.fragment or "?" in value or "#" in value:
        raise ValueError(f"{what} must not have a query or fragment")
    if not url.hostname:
        raise ValueError(f"{what} must have a host")
    if url.username is not None or url.password is not None:
        raise ValueError(f"{what} must not have credentials")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in _LOCAL_HOSTS):
        raise ValueError(f"{what} must be an https URL (http only for localhost)")
    return value.rstrip("/")


class CentralAccountConfig(BaseModel):
    """Optional link to the wristcall cloud account (OIDC). Without it the server works on its own."""

    model_config = ConfigDict(extra="forbid")
    # The wristcall Cloud URL: it signs the per-server tokens. https://...; http only for localhost/127.0.0.1;
    # kept in canonical form (scheme and host lowercased, default port and trailing slash dropped).
    issuer: str
    # The server's URL(s), exactly as apps reach it: a token is accepted only if it was made for one of them.
    audience: list[str] = Field(min_length=1)
    # Optional: the app client ids the token must come from (absent: any app of the central account).
    clients: Annotated[list[ClientId], Field(min_length=1)] | None = None
    # approval: the owner approves each new device; attestation: any linked login pairs.
    device_credential: Literal["approval", "attestation"] = "approval"

    @field_validator("issuer")
    @classmethod
    def _check_issuer(cls, value: str) -> str:
        # Canonical (lowercase scheme and host, no default port, no trailing slash): the Cloud's `iss` is in this
        # form, and the comparison with it is byte for byte.
        try:
            return normalize_audience(_check_base_url(value, "issuer"))
        except AudienceError as e:
            raise ValueError(str(e).replace("audience", "issuer", 1)) from None

    @field_validator("audience")
    @classmethod
    def _check_audience(cls, value: list[str]) -> list[str]:
        audience = [normalize_audience(url) for url in value]
        # http is only for loopback, and a server reached on loopback is a development one: mixing both would
        # let a token made for someone's localhost be good on this public server.
        if len({is_loopback(url) for url in audience}) > 1:
            raise ValueError("audience must be all loopback URLs or none")
        return audience


class PushConfig(BaseModel):
    """Push through the wristcall Cloud relay (E6). Devices and apps register their own relay key; nothing else here."""

    model_config = ConfigDict(extra="forbid")
    # The Cloud URL: https://...; http only for localhost/127.0.0.1; the trailing slash is dropped.
    relay_url: str
    timeout_s: float = Field(default=10.0, gt=0, le=30)

    @field_validator("relay_url")
    @classmethod
    def _check_relay_url(cls, value: str) -> str:
        return _check_base_url(value, "relay_url")


_NODE_SECRET = re.compile(r"wc_node_[A-Za-z0-9_-]{43}")
_NODE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
# Cloud-mode node: longest conversation when the operator does not say (a call is billed by wall time).
CLOUD_NODE_MAX_CONVERSATION_S = 3600


class CloudNodeConfig(BaseModel):
    """How a cloud-mode node (server.mode: cloud) reaches the Wristcall Cloud, which holds its data."""

    model_config = ConfigDict(extra="forbid")
    # The Cloud's base URL for /v1/node/*: https, or plain http on a private overlay network (Swarm); no trailing slash.
    cloud_url: str
    # The name this node was registered under in the Cloud.
    node_id: str
    # `wc_node_` + 43 characters: sent only in the Authorization header of requests to the Cloud.
    node_secret: SecretStr
    # 32 random bytes in base64: seals the agents' secrets (API keys) before they reach the Cloud. Not the history key.
    seal_key: SecretStr
    heartbeat_s: float = Field(default=15.0, gt=0, le=60)

    @field_validator("cloud_url")
    @classmethod
    def _check_cloud_url(cls, value: str) -> str:
        # Errors never repeat the value: it may carry credentials.
        msg = "cloud_url must be an http(s) URL with a host and no credentials, query or fragment"
        value = value.strip()
        try:
            url = urlsplit(value)
            url.port  # noqa: B018  (raises on an invalid port)
        except ValueError:
            raise ValueError(msg) from None
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or any(c in value for c in "?#@\\ ")
        ):
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("node_id")
    @classmethod
    def _check_node_id(cls, value: str) -> str:
        if not _NODE_ID.fullmatch(value):
            raise ValueError("node_id must be 1 to 32 characters: lowercase letters, digits and hyphens, a letter first")
        return value

    @field_validator("node_secret")
    @classmethod
    def _check_node_secret(cls, value: SecretStr) -> SecretStr:
        if not _NODE_SECRET.fullmatch(value.get_secret_value()):
            raise ValueError("node_secret must be wc_node_ followed by 43 characters (the secret the Cloud gave this node)")
        return value

    @field_validator("seal_key")
    @classmethod
    def _check_seal_key(cls, value: SecretStr) -> SecretStr:
        try:
            parse_key(value.get_secret_value())
        except HistoryKeyError:
            raise ValueError("seal_key must be 32 random bytes in base64") from None
        return value

    def seal(self) -> bytes:
        """The 32-byte key that seals the agents' secrets."""
        return parse_key(self.seal_key.get_secret_value())


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: ServerConfig
    providers: dict[str, ProviderConfig]
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    central_account: CentralAccountConfig | None = None
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    push: PushConfig | None = None
    cloud_node: CloudNodeConfig | None = None

    @property
    def is_cloud(self) -> bool:
        return self.server.mode == "cloud"

    @model_validator(mode="after")
    def _check_cloud_mode(self) -> "AppConfig":
        if not self.is_cloud:
            if self.cloud_node is not None:
                raise ValueError("cloud_node is only for server.mode: cloud")
            return self
        # Imported here: providers imports this module.
        from .providers import provider_kind

        if self.cloud_node is None:
            raise ValueError("server.mode: cloud needs the cloud_node block")
        if self.central_account is None:
            raise ValueError("server.mode: cloud needs central_account")
        try:
            public_url = normalize_audience(self.server.public_url)
        except AudienceError:
            raise ValueError("server.mode: cloud needs a server.public_url that is a valid audience") from None
        if self.central_account.audience != [public_url]:
            raise ValueError("server.mode: cloud needs central_account.audience to be exactly [server.public_url]")
        history_key = self.history.key()
        if history_key is None:
            raise ValueError("server.mode: cloud needs history.encryption_key")
        if self.cloud_node.seal() == history_key:
            raise ValueError("cloud_node.seal_key must differ from history.encryption_key")
        if self.push is not None:
            raise ValueError("server.mode: cloud does not take a push block (the Cloud sends the push)")
        for name, provider in self.providers.items():
            if provider_kind(provider.type) not in ("stt", "tts"):
                raise ValueError(f"server.mode: cloud takes only stt and tts providers (provider '{name}')")
        # Forced: untrusted users, so own endpoints are public only and one-way agents need no action. The call limits
        # and the conversation cap are the operator's when set; otherwise the node's starting values.
        limits = self.limits
        calls = limits.max_concurrent_calls.model_copy(
            update={
                kind: CLOUD_NODE_CONCURRENT_CALLS[kind]
                for kind in CLOUD_NODE_CONCURRENT_CALLS
                if getattr(limits.max_concurrent_calls, kind) is None
            }
        )
        # In place: a copy returned from an "after" validator is ignored when the model is built by __init__.
        self.limits = limits.model_copy(
            update={
                "public_only": True,
                "history_only_one_way": True,
                "max_concurrent_calls": calls,
                "max_conversation_s": limits.max_conversation_s or CLOUD_NODE_MAX_CONVERSATION_S,
            }
        )
        return self

    @model_validator(mode="after")
    def _check_references(self) -> "AppConfig":
        for pname, p in self.profiles.items():
            for stage in ("stt", "responder", "tts"):
                ref = getattr(p, stage)
                if ref not in self.providers:
                    raise ValueError(f"profile {pname}: provider '{ref}' ({stage}) does not exist in providers")
        return self


def _merge_profiles(raw: dict[str, Any]) -> dict[str, Any]:
    if "default" not in raw:
        raise ConfigError("profiles.default is required")
    base = raw["default"] or {}
    if not isinstance(base, dict):
        raise ConfigError("profile default must be a mapping")
    for key in _NESTED:
        if not isinstance(base.get(key) or {}, dict):
            raise ConfigError(f"profile default: {key} must be a mapping")
    out: dict[str, Any] = {"default": base}
    for name, over in raw.items():
        if name == "default":
            continue
        over = over or {}
        if not isinstance(over, dict):
            raise ConfigError(f"profile {name} must be a mapping")
        merged = {**base, **over}
        for key in _NESTED:
            if not isinstance(over.get(key) or {}, dict):
                raise ConfigError(f"profile {name}: {key} must be a mapping")
            if key in base or key in over:
                merged[key] = {**(base.get(key) or {}), **(over.get(key) or {})}
        out[name] = merged
    return out


def parse_config(data: dict[str, Any], env: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if env is None else env
    data = _interpolate(data, env)
    if data.get("profiles"):  # absent, null or {}: no legacy profiles
        if not isinstance(data["profiles"], dict):
            raise ConfigError("profiles must be a mapping")
        data = {**data, "profiles": _merge_profiles(data["profiles"])}
    else:
        data = {key: value for key, value in data.items() if key != "profiles"}
    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        # no input_value: the already interpolated values may contain secrets
        msg = "; ".join(
            f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors(include_input=False)
        )
        raise ConfigError(msg) from None


def load_config(path: Path, env: Mapping[str, str] | None = None) -> AppConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("config must be a YAML mapping")
    return parse_config(data, env)
