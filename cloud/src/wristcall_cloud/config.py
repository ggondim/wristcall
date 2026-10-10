"""Configuration from the environment. Error messages name the variable, never its value (it may hold a password)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .audience import AudienceError, normalize_audience
from .signing import SigningKey

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
    # Per-server tokens: the Cloud signs them as `public_url` (its own issuer URL) with this EC P-256 key (PEM).
    public_url: str | None = None
    signing_key_pem: bytes | None = field(default=None, repr=False)
    server_token_ttl_s: int = 300
    # Lets clients ask for tokens meant for http://localhost and friends (development only).
    allow_loopback_audience: bool = False


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


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = (_get(env, name) or "0").lower()
    if raw not in ("0", "1", "false", "true"):
        raise ConfigError(f"{name} must be 0 or 1")
    return raw in ("1", "true")


def _secret(env: Mapping[str, str], name: str) -> bytes | None:
    """A secret given inline (`NAME`) or as a file path (`NAME_FILE`), not both."""
    file_name = f"{name}_FILE"
    inline, path = _get(env, name), _get(env, file_name)
    if inline is not None and path is not None:
        raise ConfigError(f"set either {name} or {file_name}, not both")
    if inline is not None:
        return inline.encode()
    if path is None:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise ConfigError(f"{file_name} cannot be read") from None
    if not data.strip():
        raise ConfigError(f"{file_name} is empty")
    return data


def _https_url(env: Mapping[str, str], name: str, default: str | None) -> str | None:
    raw = _get(env, name)
    if raw is None:
        return default
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


def _issuer(env: Mapping[str, str]) -> str:
    return _https_url(env, "WRISTCALL_CLOUD_ISSUER", None) or CloudConfig.issuer


def same_url(a: str, b: str) -> bool:
    """Whether two URLs name the same place, compared in their audience form when they have one."""
    try:
        return normalize_audience(a) == normalize_audience(b)
    except AudienceError:
        return a.rstrip("/") == b.rstrip("/")


def check_public_url(public_url: str, issuer: str) -> None:
    """The Cloud's issuer URL is the `iss` of every per-server token and servers compare it byte for byte: it must
    already be in its canonical (audience) form, and it must not be the account issuer."""
    try:
        canonical = normalize_audience(public_url) == public_url
    except AudienceError:
        canonical = False
    if not canonical:
        raise ConfigError(
            "WRISTCALL_CLOUD_PUBLIC_URL must be a canonical https URL "
            "(lowercase host, no default port, no trailing slash)"
        )
    if same_url(public_url, issuer):
        raise ConfigError("WRISTCALL_CLOUD_PUBLIC_URL must differ from WRISTCALL_CLOUD_ISSUER")


def _server_tokens(env: Mapping[str, str], issuer: str) -> tuple[str | None, bytes | None]:
    public_url = _https_url(env, "WRISTCALL_CLOUD_PUBLIC_URL", None)
    pem = _secret(env, "WRISTCALL_CLOUD_SIGNING_KEY")
    if (public_url is None) != (pem is None):
        raise ConfigError("WRISTCALL_CLOUD_PUBLIC_URL and WRISTCALL_CLOUD_SIGNING_KEY go together")
    if public_url is not None:
        check_public_url(public_url, issuer)
    if pem is not None:
        try:
            SigningKey(pem)
        except ValueError:
            raise ConfigError("WRISTCALL_CLOUD_SIGNING_KEY must be an EC P-256 private key (PEM)") from None
    return public_url, pem


def config_from_env(env: Mapping[str, str]) -> CloudConfig:
    mongo_url = _get(env, "WRISTCALL_CLOUD_MONGO_URL")
    if mongo_url is None:
        raise ConfigError("WRISTCALL_CLOUD_MONGO_URL is required")
    if not mongo_url.startswith(("mongodb://", "mongodb+srv://")):
        raise ConfigError("WRISTCALL_CLOUD_MONGO_URL must be a mongodb:// or mongodb+srv:// URL")
    clients = {kind: value for kind, var in CLIENT_VARS.items() if (value := _get(env, var)) is not None}
    if not clients:
        raise ConfigError("at least one of " + ", ".join(CLIENT_VARS.values()) + " is required")
    issuer = _issuer(env)
    public_url, signing_key_pem = _server_tokens(env, issuer)
    return CloudConfig(
        mongo_url=mongo_url,
        database=_get(env, "WRISTCALL_CLOUD_DATABASE") or CloudConfig.database,
        issuer=issuer,
        clients=clients,
        project_id=_get(env, "WRISTCALL_CLOUD_PROJECT_ID"),
        max_servers=_positive_int(env, "WRISTCALL_CLOUD_MAX_SERVERS", CloudConfig.max_servers),
        max_agents_per_server=_positive_int(env, "WRISTCALL_CLOUD_MAX_AGENTS_PER_SERVER", CloudConfig.max_agents_per_server),
        public_url=public_url,
        signing_key_pem=signing_key_pem,
        allow_loopback_audience=_flag(env, "WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE"),
    )
