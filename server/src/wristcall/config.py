"""Operator configuration: YAML with ${ENV} references. Server, providers on offer and limits.

`profiles` (inheriting from `default`) is the 0.2.0 format: imported once as agents (see bootstrap.py).
"""

import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


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


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    language: str = "en"
    stt: str
    responder: str
    tts: str
    system_prompt: str = ""
    fallback_message: str = "Sorry, I couldn't answer right now."
    vad: VadConfig = Field(default_factory=VadConfig)
    timeouts: Timeouts = Field(default_factory=Timeouts)


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


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: ServerConfig
    providers: dict[str, ProviderConfig]
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)

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
