"""Operator configuration: YAML with ${ENV} references. Server, providers on offer and limits.

`profiles` (inheriting from `default`) is the 0.2.0 format: imported once as agents (see bootstrap.py).
"""

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from .audience import is_loopback, normalize_audience
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


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_url: str
    directory_url: str | None = None
    pairing_approval: Literal["code", "manual"] = "code"
    data_dir: Path = Path("/data")
    client_ip_header: str | None = None


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_agents_per_user: int = Field(default=20, ge=1)
    max_devices_per_user: int = Field(default=10, ge=1)
    # Agents may point at the user's own STT/action/TTS URLs. Turn off on a server with untrusted users:
    # the server would make requests to any URL they give, including the internal network.
    custom_endpoints: bool = True
    # Provider types a custom endpoint may use. The fake types (tests) can be made to allocate huge buffers.
    custom_endpoint_types: list[str] = Field(
        default_factory=lambda: ["openai_stt", "openai_chat", "openai_tts", "webhook"]
    )
    # Longest one-way call (one-shot or monologue): the server stops recording and delivers what it got.
    max_one_way_call_s: int = Field(default=1800, ge=60, le=14_400)


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


class CentralAccountConfig(BaseModel):
    """Optional link to the wristcall cloud account (OIDC). Without it the server works on its own."""

    model_config = ConfigDict(extra="forbid")
    # The wristcall Cloud URL: it signs the per-server tokens. https://...; http only for localhost/127.0.0.1;
    # the trailing slash is dropped.
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
        value = value.strip()
        url = urlsplit(value)
        if url.query or url.fragment or "?" in value or "#" in value:
            raise ValueError("issuer must not have a query or fragment")
        if not url.hostname:
            raise ValueError("issuer must have a host")
        if url.username is not None or url.password is not None:
            raise ValueError("issuer must not have credentials")
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in _LOCAL_HOSTS):
            raise ValueError("issuer must be an https URL (http only for localhost)")
        return value.rstrip("/")

    @field_validator("audience")
    @classmethod
    def _check_audience(cls, value: list[str]) -> list[str]:
        audience = [normalize_audience(url) for url in value]
        # http is only for loopback, and a server reached on loopback is a development one: mixing both would
        # let a token made for someone's localhost be good on this public server.
        if len({is_loopback(url) for url in audience}) > 1:
            raise ValueError("audience must be all loopback URLs or none")
        return audience


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: ServerConfig
    providers: dict[str, ProviderConfig]
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    central_account: CentralAccountConfig | None = None
    history: HistoryConfig = Field(default_factory=HistoryConfig)

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
