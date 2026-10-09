"""Configuration from the environment. Error messages name the variable, never its value (it may hold a password)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CLIENT_VARS = {"ios": "WRISTCALL_CLOUD_CLIENT_IOS", "pwa": "WRISTCALL_CLOUD_CLIENT_PWA", "watch": "WRISTCALL_CLOUD_CLIENT_WATCH"}


@dataclass(frozen=True)
class CloudConfig:
    mongo_url: str
    database: str = "wristcall_cloud"
    issuer: str = "https://auth.trigram.com.br"
    # {"ios": id, "pwa": id, "watch": id}: tokens must be meant for (audience) and issued to (client) one of these.
    clients: dict[str, str] = field(default_factory=dict)
    project_id: str | None = None
    max_servers: int = 20
    max_agents_per_server: int = 50


class ConfigError(Exception):
    pass


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigError(f"{name} must be a positive integer")
    return int(raw)


def _issuer(env: Mapping[str, str]) -> str:
    name = "WRISTCALL_CLOUD_ISSUER"
    raw = _get(env, name)
    if raw is None:
        return CloudConfig.issuer
    try:
        parts = urlsplit(raw)
        host = parts.hostname
    except ValueError:
        raise ConfigError(f"{name} must be an https URL") from None
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(f"{name} must be an https URL")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in LOCAL_HOSTS):
        raise ConfigError(f"{name} must be an https URL (http only for localhost)")
    return raw.rstrip("/")


def config_from_env(env: Mapping[str, str]) -> CloudConfig:
    mongo_url = _get(env, "WRISTCALL_CLOUD_MONGO_URL")
    if mongo_url is None:
        raise ConfigError("WRISTCALL_CLOUD_MONGO_URL is required")
    if not mongo_url.startswith(("mongodb://", "mongodb+srv://")):
        raise ConfigError("WRISTCALL_CLOUD_MONGO_URL must be a mongodb:// or mongodb+srv:// URL")
    clients = {kind: value for kind, var in CLIENT_VARS.items() if (value := _get(env, var)) is not None}
    if not clients:
        raise ConfigError("at least one of " + ", ".join(CLIENT_VARS.values()) + " is required")
    return CloudConfig(
        mongo_url=mongo_url,
        database=_get(env, "WRISTCALL_CLOUD_DATABASE") or CloudConfig.database,
        issuer=_issuer(env),
        clients=clients,
        project_id=_get(env, "WRISTCALL_CLOUD_PROJECT_ID"),
        max_servers=_positive_int(env, "WRISTCALL_CLOUD_MAX_SERVERS", CloudConfig.max_servers),
        max_agents_per_server=_positive_int(env, "WRISTCALL_CLOUD_MAX_AGENTS_PER_SERVER", CloudConfig.max_agents_per_server),
    )
