"""Server configuration: YAML with ${ENV} references and profiles that inherit from `default`."""

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
    model_config = ConfigDict(extra="forbid")
    type: Literal["silero", "energy"] = "silero"
    threshold: float = 0.5
    energy_dbfs: float = -45.0
    silence_ms: int = 800
    min_speech_ms: int = 300
    max_turn_ms: int = 60_000
    pre_roll_ms: int = 300


class Timeouts(BaseModel):
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
    vad: VadConfig = Field(default_factory=VadConfig)
    timeouts: Timeouts = Field(default_factory=Timeouts)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_url: str
    directory_url: str | None = None
    pairing_approval: Literal["code", "manual"] = "code"
    data_dir: Path = Path("/data")
    client_ip_header: str | None = None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: ServerConfig
    providers: dict[str, ProviderConfig]
    profiles: dict[str, ProfileConfig]

    @model_validator(mode="after")
    def _check_references(self) -> "AppConfig":
        if "default" not in self.profiles:
            raise ValueError("profiles.default is required")
        for pname, p in self.profiles.items():
            for stage in ("stt", "responder", "tts"):
                ref = getattr(p, stage)
                if ref not in self.providers:
                    raise ValueError(f"profile {pname}: provider '{ref}' ({stage}) does not exist in providers")
        return self

    def profile(self, name: str | None) -> tuple[str, ProfileConfig]:
        key = name or "default"
        if key not in self.profiles:
            raise KeyError(key)
        return key, self.profiles[key]


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
    if not isinstance(data.get("profiles"), dict):
        raise ConfigError("profiles section is missing")
    data = {**data, "profiles": _merge_profiles(data["profiles"])}
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
