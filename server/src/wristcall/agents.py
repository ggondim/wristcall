"""Agents: what a user calls from the watch. Spec validation, CRUD rules and providers per call."""

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from .config import AppConfig, HistoryConfig, ProfileConfig, ProviderConfig, Timeouts, VadConfig
from .protocol import TurnEnd
from .providers import Kind, ProviderError, ProviderSet, SpeechToText, Webhook, build_provider, provider_kind
from .providers.webhook import check_url
from .storage import AgentRecord, Conflict, LimitReached, Storage

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ICON = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)*$")
DEFAULT_ICON = "waveform"
CallType = Literal["conversation", "one-shot", "monologue"]
# Days the agent's calls stay in the history; "forever"; None = the operator's default. The ceiling applies to all.
RetentionDays = Annotated[StrictInt, Field(ge=1, le=36_500)] | Literal["forever"] | None
# One-way calls: the transcript goes to a webhook (the action); no answer, no voice.
ONE_WAY = frozenset({"one-shot", "monologue"})
REDACTED = "***"
_OPTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# A key is secret when its name contains one of these (api_key, x-api-key, accesstoken, client_secret, Set-Cookie...).
# Over-redaction is harmless: sending `***` back keeps the stored value.
_SECRET_MARKS = ("key", "token", "secret", "passw", "pwd", "auth", "credential", "cookie", "passphrase")
# Counts and tokenizers stay visible (max_tokens, max_completion_tokens, min_tokens, tokenizer), unless
# another mark is left in the name.
_TOKEN_COUNT = re.compile(r"(max|min)[\w-]*tokens?")


def _is_secret(key: str) -> bool:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().replace("tokeniz", "")
    if _TOKEN_COUNT.fullmatch(name):
        name = re.sub(r"tokens?$", "", name)
    return any(mark in name for mark in _SECRET_MARKS)


# Agent field → provider kind in the YAML registry ("action" is the responder).
STAGES: dict[str, Kind] = {"stt": "stt", "action": "responder", "tts": "tts"}
ONE_WAY_STAGES: dict[str, Kind] = {"stt": "stt", "action": "webhook"}


def stage_kinds(call_type: str) -> dict[str, Kind]:
    """The fields a call type needs and their provider kinds. A one-way agent's action is a webhook, and it has no tts."""
    return ONE_WAY_STAGES if call_type in ONE_WAY else STAGES


class ProviderRef(BaseModel):
    """A provider the operator offers in the YAML, by name."""

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)


class CustomEndpoint(BaseModel):
    """The user's own URL: a provider type plus its options (base_url, model, api_key...)."""

    model_config = ConfigDict(extra="allow")
    type: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "CustomEndpoint":
        options = self.model_extra or {}
        if "provider" in options:
            raise ValueError("use either 'provider' or 'type', not both")
        if "warmup" in options:
            raise ValueError("warmup is an operator setting, not available on custom endpoints")
        url = options.get("url")
        if url is not None:
            check_url(str(url))
        base_url = options.get("base_url")
        if base_url is not None:
            # The URL is shown back to the owner as is: secrets go in options that get redacted (api_key...).
            parts = urlsplit(str(base_url))
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("base_url must be an http(s) URL")
            if parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("base_url must not hold credentials, a query or a fragment; use api_key")
        return self

    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


Endpoint = ProviderRef | CustomEndpoint


class AgentSpec(BaseModel):
    """Behaviour of the agent. Has the fields CallSession reads (language, prompts, vad, timeouts)."""

    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="en", min_length=2, max_length=16)
    stt: Endpoint
    action: Endpoint
    # Absent only on one-way agents (they never speak).
    tts: Endpoint | None = None
    system_prompt: str = Field(default="", max_length=20_000)
    fallback_message: str = Field(default="Sorry, I couldn't answer right now.", min_length=1, max_length=500)
    turn_end: TurnEnd = "auto"
    vad: VadConfig = Field(default_factory=VadConfig)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    retention_days: RetentionDays = None


class AgentInput(BaseModel):
    """Create or update payload (API and CLI). Absent fields: defaults on create, unchanged on update."""

    model_config = ConfigDict(extra="forbid")
    slug: str | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    icon: str | None = Field(default=None, max_length=64)
    call_type: CallType | None = None
    position: int | None = Field(default=None, ge=0)
    language: str | None = None
    stt: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    tts: dict[str, Any] | None = None
    system_prompt: str | None = None
    fallback_message: str | None = None
    turn_end: TurnEnd | None = None
    vad: dict[str, Any] | None = None
    timeouts: dict[str, Any] | None = None
    retention_days: RetentionDays = None  # on update, an explicit null goes back to the operator's default


@dataclass(frozen=True)
class Agent:
    id: str
    user_id: str
    slug: str
    display_name: str
    icon: str
    call_type: str
    position: int
    spec: AgentSpec
    created_at: float
    updated_at: float

    @classmethod
    def from_record(cls, r: AgentRecord) -> "Agent":
        return cls(
            id=r.id, user_id=r.user_id, slug=r.slug, display_name=r.display_name, icon=r.icon,
            call_type=r.call_type, position=r.position, spec=AgentSpec.model_validate(r.spec),
            created_at=r.created_at, updated_at=r.updated_at,
        )

    def to_record(self) -> AgentRecord:
        return AgentRecord(
            id=self.id, user_id=self.user_id, slug=self.slug, display_name=self.display_name, icon=self.icon,
            call_type=self.call_type, position=self.position, spec=self.spec.model_dump(mode="json"),
            created_at=self.created_at, updated_at=self.updated_at,
        )


class AgentError(Exception):
    """code: invalid | not_found | conflict | limit | unsupported."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _pydantic_message(e: ValidationError) -> str:
    # No input values: they may hold API keys.
    return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors(include_input=False))


def resolve_endpoint(config: AppConfig, endpoint: Endpoint, kind: Kind) -> tuple[str, ProviderConfig]:
    """Name (for messages) and provider config of an endpoint. Raises ProviderError when unusable."""
    if isinstance(endpoint, ProviderRef):
        pcfg = config.providers.get(endpoint.provider)
        if pcfg is None:
            raise ProviderError(f"provider '{endpoint.provider}' does not exist on this server")
        name = endpoint.provider
    else:
        if not config.limits.custom_endpoints:
            raise ProviderError("this server only accepts the providers it offers (custom endpoints are off)")
        if endpoint.type not in config.limits.custom_endpoint_types:
            raise ProviderError(f"type '{endpoint.type}' is not allowed for custom endpoints on this server")
        try:
            pcfg = ProviderConfig(type=endpoint.type, **endpoint.options())
        except Exception:
            # Never echo the options: they may hold API keys.
            raise ProviderError(f"invalid options for type '{endpoint.type}'") from None
        name = f"custom {endpoint.type}"
    actual = provider_kind(pcfg.type)
    if actual is None:
        raise ProviderError(f"provider '{name}': unknown type '{pcfg.type}'")
    if actual != kind:
        raise ProviderError(f"provider '{name}' is {actual}, not {kind}")
    return name, pcfg


def build_endpoint(config: AppConfig, endpoint: Endpoint, kind: Kind, http: httpx.AsyncClient) -> Any:
    """Provider instance of an endpoint. Raises ProviderError; for custom endpoints, never with option values."""
    name, pcfg = resolve_endpoint(config, endpoint, kind)
    if isinstance(endpoint, ProviderRef):
        return build_provider(name, pcfg, kind, http)
    try:
        return build_provider(name, pcfg, kind, http)
    except Exception as e:
        cause = e.__cause__ if isinstance(e, ProviderError) else e
        missing = cause.args[0] if isinstance(cause, KeyError) and cause.args else None
        # Only name an option-like key the user did not send and that appears nowhere in the values.
        if isinstance(missing, str) and _OPTION_NAME.fullmatch(missing) and missing not in repr(endpoint.options()):
            raise ProviderError(f"missing option '{missing}' for type '{endpoint.type}'") from None
        raise ProviderError(f"invalid options for type '{endpoint.type}'") from None


def build_agent_providers(config: AppConfig, spec: AgentSpec, http: httpx.AsyncClient) -> ProviderSet:
    if spec.tts is None:
        raise ProviderError("a conversation needs a tts")
    built: dict[str, Any] = {}
    for field, kind in STAGES.items():
        built[field] = build_endpoint(config, getattr(spec, field), kind, http)
    return ProviderSet(stt=built["stt"], responder=built["action"], tts=built["tts"])


@dataclass
class OneWayProviders:
    stt: SpeechToText
    webhook: Webhook


def build_one_way_providers(config: AppConfig, spec: AgentSpec, http: httpx.AsyncClient) -> OneWayProviders:
    return OneWayProviders(
        stt=build_endpoint(config, spec.stt, "stt", http),
        webhook=build_endpoint(config, spec.action, "webhook", http),
    )


def _secret(key: Any, value: Any, parent: Any = None) -> bool:
    """Secret-looking keys, and every value of `headers` (a webhook's auth header may have any name)."""
    return value not in (None, "") and (_is_secret(str(key)) or parent == "headers")


def _redact(value: Any, parent: Any = None) -> Any:
    """Secret-looking keys at any depth (extra_body, extra_form...) and header values become `***`."""
    if isinstance(value, dict):
        return {k: REDACTED if _secret(k, v, parent) else _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def redact_endpoint(endpoint: Endpoint | None) -> dict[str, Any] | None:
    return None if endpoint is None else _redact(endpoint.model_dump())


def _restore(new: Any, old: Any, parent: Any = None) -> Any:
    """Puts the stored value back wherever an update sent `***` for a secret key that holds one."""
    if isinstance(new, dict) and isinstance(old, dict):
        return {
            k: old[k] if v == REDACTED and _secret(k, old.get(k), parent) else _restore(v, old.get(k), k)
            for k, v in new.items()
        }
    return new


def _keep_redacted(new: dict[str, Any], old: Endpoint | None) -> dict[str, Any]:
    """`***` in an update means "keep the stored secret" (show → edit → apply round trips), same type only."""
    if not isinstance(old, CustomEndpoint) or new.get("type") != old.type:
        return new
    return _restore(new, old.model_dump())


def _has_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(v) for v in value)
    return value == REDACTED


def agent_summary(agent: Agent) -> dict[str, Any]:
    """What a device sees: enough to list and call."""
    return {
        "id": agent.id,
        "slug": agent.slug,
        "display_name": agent.display_name,
        "icon": agent.icon,
        "call_type": agent.call_type,
        "turn_end": agent.spec.turn_end,
    }


def agent_detail(agent: Agent, history: HistoryConfig | None = None) -> dict[str, Any]:
    """What the owner sees through the API or the CLI; secrets redacted. With the operator's history settings, also
    the retention in force (null: kept until deleted)."""
    spec = agent.spec
    extra = {} if history is None else {"effective_retention_days": history.effective_days(spec.retention_days)}
    return {
        **agent_summary(agent),
        "position": agent.position,
        "language": spec.language,
        "stt": redact_endpoint(spec.stt),
        "action": redact_endpoint(spec.action),
        "tts": redact_endpoint(spec.tts),
        "system_prompt": spec.system_prompt,
        "fallback_message": spec.fallback_message,
        "vad": spec.vad.model_dump(),
        "timeouts": spec.timeouts.model_dump(),
        "retention_days": spec.retention_days,
        **extra,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _clamp(model: type[BaseModel], values: dict[str, Any], prefix: str, adjusted: list[str]) -> dict[str, Any]:
    """Moves each number into the model's bounds; past an exclusive bound (gt) it falls back to the default."""
    out = dict(values)
    for name, info in model.model_fields.items():
        value = out.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        new = value
        for bound in info.metadata:  # pydantic keeps Field(ge=..., le=..., gt=...) here
            if getattr(bound, "ge", None) is not None and new < bound.ge:
                new = bound.ge
            elif getattr(bound, "le", None) is not None and new > bound.le:
                new = bound.le
            elif getattr(bound, "gt", None) is not None and new <= bound.gt:
                new = info.default
        if new != value:
            out[name] = new
            adjusted.append(f"{prefix}.{name}")
    return out


def legacy_spec(profile: ProfileConfig) -> tuple[AgentSpec, list[str]]:
    """Agent spec of a 0.2.0 profile, which had no bounds: values outside the agent's bounds are adjusted.

    Also returns the names of the adjusted fields (never their values: they go to the log).
    """
    adjusted: list[str] = []
    language = profile.language
    if not 2 <= len(language) <= 16:
        language = "en"
        adjusted.append("language")
    system_prompt = profile.system_prompt
    if len(system_prompt) > 20_000:
        system_prompt = system_prompt[:20_000]
        adjusted.append("system_prompt")
    fallback = profile.fallback_message
    if not fallback or len(fallback) > 500:
        fallback = fallback[:500] or AgentSpec.model_fields["fallback_message"].default
        adjusted.append("fallback_message")
    spec = AgentSpec(
        language=language,
        stt=ProviderRef(provider=profile.stt),
        action=ProviderRef(provider=profile.responder),
        tts=ProviderRef(provider=profile.tts),
        system_prompt=system_prompt,
        fallback_message=fallback,
        vad=VadConfig(**_clamp(VadConfig, profile.vad.model_dump(), "vad", adjusted)),
        timeouts=Timeouts(**_clamp(Timeouts, profile.timeouts.model_dump(), "timeouts", adjusted)),
    )
    return spec, adjusted


def spec_from_profile(profile: ProfileConfig) -> AgentSpec:
    return legacy_spec(profile)[0]


def retention_seconds(days: int | None) -> float | None:
    return None if days is None else days * 86_400.0


def new_agent_id() -> str:
    return f"ag_{secrets.token_hex(6)}"


class AgentService:
    def __init__(
        self, storage: Storage, config: AppConfig, http: httpx.AsyncClient, *, now: Callable[[], float] = time.time
    ) -> None:
        self._st = storage
        self._config = config
        self._http = http
        self._now = now

    async def list(self, user_id: str) -> list[Agent]:
        return [Agent.from_record(r) for r in await self._st.agents.list(user_id)]

    async def get(self, user_id: str, ref: str) -> Agent:
        record = await self._st.agents.get(user_id, ref)
        if record is None:
            raise AgentError("not_found", f"agent not found: {ref}")
        return Agent.from_record(record)

    async def default(self, user_id: str) -> Agent | None:
        records = await self._st.agents.list(user_id)
        return Agent.from_record(records[0]) if records else None

    def default_endpoint(self, field: str, call_type: str = "conversation") -> dict[str, Any]:
        """The only provider of that kind in the YAML; otherwise the caller must choose."""
        kind = stage_kinds(call_type)[field]
        names = [n for n, p in self._config.providers.items() if provider_kind(p.type) == kind]
        if len(names) != 1:
            offered = ", ".join(names) or "none"
            raise AgentError("invalid", f"{field}: choose a provider (this server offers: {offered})")
        return {"provider": names[0]}

    def _check(self, slug: str, icon: str, call_type: str, spec: AgentSpec) -> None:
        if not SLUG.fullmatch(slug):
            raise AgentError("invalid", "slug: use 1 to 32 lowercase letters, digits or hyphens, starting with a letter or digit")
        if not ICON.fullmatch(icon):
            raise AgentError("invalid", "icon: use an SF Symbol name such as 'waveform' or 'person.wave.2'")
        kinds = dict(stage_kinds(call_type))
        if call_type in ONE_WAY and spec.tts is not None:
            # Kept for a switch back to conversation; must stay valid while stored.
            kinds["tts"] = "tts"
        for field, kind in kinds.items():
            endpoint = getattr(spec, field)
            if endpoint is None:
                raise AgentError("invalid", f"{field}: required for {call_type} agents")
            if _has_placeholder(endpoint.model_dump()):
                raise AgentError("invalid", f"{field}: '{REDACTED}' only keeps a secret already stored for this endpoint")
            try:
                build_endpoint(self._config, endpoint, kind, self._http)
            except ProviderError as e:
                raise AgentError("invalid", f"{field}: {e}") from None

    @staticmethod
    def _parse_input(data: dict[str, Any]) -> AgentInput:
        try:
            return AgentInput.model_validate(data)
        except ValidationError as e:
            raise AgentError("invalid", _pydantic_message(e)) from None

    @staticmethod
    def _spec(data: dict[str, Any]) -> AgentSpec:
        try:
            return AgentSpec.model_validate(data)
        except ValidationError as e:
            raise AgentError("invalid", _pydantic_message(e)) from None

    async def create(self, user_id: str, data: dict[str, Any]) -> Agent:
        inp = self._parse_input(data)
        if inp.slug is None:
            raise AgentError("invalid", "slug is required")
        given = inp.model_dump(exclude_none=True, exclude={"slug", "display_name", "icon", "call_type", "position"})
        call_type = inp.call_type or "conversation"
        for field in stage_kinds(call_type):
            if field not in given:
                given[field] = self.default_endpoint(field, call_type)
        spec = self._spec(given)
        icon = inp.icon or DEFAULT_ICON
        self._check(inp.slug, icon, call_type, spec)
        now = self._now()
        agent = Agent(
            id=new_agent_id(), user_id=user_id, slug=inp.slug, display_name=inp.display_name or inp.slug,
            icon=icon, call_type=call_type, position=0, spec=spec, created_at=now, updated_at=now,
        )
        limit = self._config.limits.max_agents_per_user
        try:
            created = Agent.from_record(await self._st.agents.create(agent.to_record(), max_count=limit))
        except Conflict:
            raise AgentError("conflict", f"an agent with slug '{inp.slug}' already exists") from None
        except LimitReached:
            raise AgentError("limit", f"agent limit reached ({limit})") from None
        if inp.position is not None:
            return await self._move(user_id, created.id, inp.position)
        return created

    async def update(self, user_id: str, ref: str, data: dict[str, Any]) -> Agent:
        inp = self._parse_input(data)
        current = await self.get(user_id, ref)
        merged = current.spec.model_dump(mode="json")
        for field in ("language", "system_prompt", "fallback_message", "turn_end"):
            value = getattr(inp, field)
            if value is not None:
                merged[field] = value
        for field in STAGES:
            value = getattr(inp, field)
            if value is not None:
                merged[field] = _keep_redacted(value, getattr(current.spec, field))
            elif field == "tts" and "tts" in inp.model_fields_set:
                merged["tts"] = None  # an explicit null drops the voice (one-way agents only; _check enforces it)
        if "retention_days" in inp.model_fields_set:
            merged["retention_days"] = inp.retention_days
        for field in ("vad", "timeouts"):
            value = getattr(inp, field)
            if value is not None:
                merged[field] = {**merged[field], **value}
        spec = self._spec(merged)
        slug = inp.slug or current.slug
        icon = inp.icon or current.icon
        call_type = inp.call_type or current.call_type
        self._check(slug, icon, call_type, spec)
        updated = Agent(
            id=current.id, user_id=user_id, slug=slug, display_name=inp.display_name or current.display_name,
            icon=icon, call_type=call_type, position=current.position,
            spec=spec, created_at=current.created_at, updated_at=self._now(),
        )
        try:
            saved = Agent.from_record(await self._st.agents.update(updated.to_record()))
        except Conflict:
            raise AgentError("conflict", f"an agent with slug '{slug}' already exists") from None
        except KeyError:
            raise AgentError("not_found", f"agent not found: {ref}") from None
        if saved.spec.retention_days != current.spec.retention_days:
            await self._st.calls.set_expiry(
                user_id, saved.id, retention_seconds(self._config.history.effective_days(saved.spec.retention_days)),
            )
        if inp.position is not None:
            return await self._move(user_id, saved.id, inp.position)
        return saved

    async def _move(self, user_id: str, agent_id: str, index: int) -> Agent:
        moved = await self._st.agents.move(user_id, agent_id, index)
        if moved is None:
            raise AgentError("not_found", f"agent not found: {agent_id}")
        return Agent.from_record(moved)

    async def delete(self, user_id: str, ref: str) -> None:
        agent = await self.get(user_id, ref)
        if not await self._st.agents.delete(user_id, agent.id):
            raise AgentError("not_found", f"agent not found: {ref}")
