"""Agents: what a user calls from the watch. Spec validation, CRUD rules and providers per call."""

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import AppConfig, ProfileConfig, ProviderConfig, Timeouts, VadConfig
from .protocol import TurnEnd
from .providers import Kind, ProviderError, ProviderSet, build_provider, provider_kind
from .storage import AgentRecord, Conflict, Storage

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ICON = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)*$")
DEFAULT_ICON = "waveform"
CallType = Literal["conversation", "one-shot", "monologue"]
# one-shot and monologue are stored already, but only arrive with epic E2.
SUPPORTED_CALL_TYPES = {"conversation"}
REDACTED = "***"
_SECRET_OPTION = re.compile(r"key|token|secret|password", re.IGNORECASE)
# Agent field → provider kind in the YAML registry ("action" is the responder).
STAGES: dict[str, Kind] = {"stt": "stt", "action": "responder", "tts": "tts"}


class ProviderRef(BaseModel):
    """A provider the operator offers in the YAML, by name."""

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)


class CustomEndpoint(BaseModel):
    """The user's own URL: a provider type plus its options (base_url, model, api_key...)."""

    model_config = ConfigDict(extra="allow")
    type: str = Field(min_length=1)

    @model_validator(mode="after")
    def _not_both(self) -> "CustomEndpoint":
        if "provider" in (self.model_extra or {}):
            raise ValueError("use either 'provider' or 'type', not both")
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
    tts: Endpoint
    system_prompt: str = Field(default="", max_length=20_000)
    fallback_message: str = Field(default="Sorry, I couldn't answer right now.", min_length=1, max_length=500)
    turn_end: TurnEnd = "auto"
    vad: VadConfig = Field(default_factory=VadConfig)
    timeouts: Timeouts = Field(default_factory=Timeouts)


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
        pcfg = ProviderConfig(type=endpoint.type, **endpoint.options())
        name = f"custom {endpoint.type}"
    actual = provider_kind(pcfg.type)
    if actual is None:
        raise ProviderError(f"provider '{name}': unknown type '{pcfg.type}'")
    if actual != kind:
        raise ProviderError(f"provider '{name}' is {actual}, not {kind}")
    return name, pcfg


def build_agent_providers(config: AppConfig, spec: AgentSpec, http: httpx.AsyncClient) -> ProviderSet:
    built: dict[str, Any] = {}
    for field, kind in STAGES.items():
        name, pcfg = resolve_endpoint(config, getattr(spec, field), kind)
        built[field] = build_provider(name, pcfg, kind, http)
    return ProviderSet(stt=built["stt"], responder=built["action"], tts=built["tts"])


def redact_endpoint(endpoint: Endpoint) -> dict[str, Any]:
    data = endpoint.model_dump()
    if isinstance(endpoint, CustomEndpoint):
        for key in endpoint.options():
            if _SECRET_OPTION.search(key):
                data[key] = REDACTED
    return data


def _keep_redacted(new: dict[str, Any], old: Endpoint) -> dict[str, Any]:
    """`***` in an update means "keep the stored secret" (show → edit → apply round trips)."""
    if not isinstance(old, CustomEndpoint) or new.get("type") != old.type:
        return new
    kept = dict(new)
    for key, value in new.items():
        if value == REDACTED and key in old.options():
            kept[key] = old.options()[key]
    return kept


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


def agent_detail(agent: Agent) -> dict[str, Any]:
    """What the owner sees through the API or the CLI; secrets redacted."""
    spec = agent.spec
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
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def spec_from_profile(profile: ProfileConfig) -> AgentSpec:
    return AgentSpec(
        language=profile.language,
        stt=ProviderRef(provider=profile.stt),
        action=ProviderRef(provider=profile.responder),
        tts=ProviderRef(provider=profile.tts),
        system_prompt=profile.system_prompt,
        fallback_message=profile.fallback_message,
        vad=profile.vad,
        timeouts=profile.timeouts,
    )


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

    def default_endpoint(self, field: str) -> dict[str, Any]:
        """The only provider of that kind in the YAML; otherwise the caller must choose."""
        kind = STAGES[field]
        names = [n for n, p in self._config.providers.items() if provider_kind(p.type) == kind]
        if len(names) != 1:
            offered = ", ".join(names) or "none"
            raise AgentError("invalid", f"{field}: choose a provider (this server offers: {offered})")
        return {"provider": names[0]}

    def _check(self, slug: str, icon: str, call_type: str, spec: AgentSpec) -> None:
        if not SLUG.match(slug):
            raise AgentError("invalid", "slug: use 1 to 32 lowercase letters, digits or hyphens, starting with a letter or digit")
        if not ICON.match(icon):
            raise AgentError("invalid", "icon: use an SF Symbol name such as 'waveform' or 'person.wave.2'")
        if call_type not in SUPPORTED_CALL_TYPES:
            raise AgentError("unsupported", f"call_type '{call_type}' is not supported yet (only conversation)")
        for field, kind in STAGES.items():
            try:
                name, pcfg = resolve_endpoint(self._config, getattr(spec, field), kind)
                build_provider(name, pcfg, kind, self._http)
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
        for field in STAGES:
            if field not in given:
                given[field] = self.default_endpoint(field)
        spec = self._spec(given)
        icon = inp.icon or DEFAULT_ICON
        call_type = inp.call_type or "conversation"
        self._check(inp.slug, icon, call_type, spec)
        if await self._st.agents.count(user_id) >= self._config.limits.max_agents_per_user:
            raise AgentError("limit", f"agent limit reached ({self._config.limits.max_agents_per_user})")
        now = self._now()
        agent = Agent(
            id=new_agent_id(), user_id=user_id, slug=inp.slug, display_name=inp.display_name or inp.slug,
            icon=icon, call_type=call_type, position=0, spec=spec, created_at=now, updated_at=now,
        )
        try:
            created = Agent.from_record(await self._st.agents.create(agent.to_record()))
        except Conflict:
            raise AgentError("conflict", f"an agent with slug '{inp.slug}' already exists") from None
        if inp.position is not None:
            return await self.update(user_id, created.id, {"position": inp.position})
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
        if inp.position is not None:
            return await self._move(user_id, saved.id, inp.position)
        return saved

    async def _move(self, user_id: str, agent_id: str, index: int) -> Agent:
        """Puts the agent at that index of the user's list (past the end = last) and renumbers 0..n-1."""
        records = await self._st.agents.list(user_id)
        moving = next(r for r in records if r.id == agent_id)
        others = [r for r in records if r.id != agent_id]
        others.insert(min(index, len(others)), moving)
        result = moving
        for position, record in enumerate(others):
            if record.position != position:
                record = await self._st.agents.update(replace(record, position=position))
            if record.id == agent_id:
                result = record
        return Agent.from_record(result)

    async def delete(self, user_id: str, ref: str) -> None:
        agent = await self.get(user_id, ref)
        if not await self._st.agents.delete(user_id, agent.id):
            raise AgentError("not_found", f"agent not found: {ref}")
