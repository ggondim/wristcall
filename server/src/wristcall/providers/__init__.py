"""Contracts for the three stages (STT, responder, TTS) and construction from the config."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from ..config import AppConfig, ProfileConfig, ProviderConfig

Kind = Literal["stt", "responder", "tts"]


class ProviderError(Exception):
    pass


class SpeechToText(Protocol):
    async def transcribe(self, wav: bytes, language: str) -> str: ...


class Responder(Protocol):
    def respond(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...


class TextToSpeech(Protocol):
    sample_rate: int

    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...


@dataclass
class ProviderSet:
    stt: SpeechToText
    responder: Responder
    tts: TextToSpeech


Factory = Callable[[dict[str, Any], httpx.AsyncClient], Any]
_REGISTRY: dict[str, tuple[Kind, Factory]] = {}


def register(type_name: str, kind: Kind) -> Callable[[Factory], Factory]:
    def deco(factory: Factory) -> Factory:
        _REGISTRY[type_name] = (kind, factory)
        return factory

    return deco


def auth_headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _load_builtin() -> None:
    from . import fake, openai_chat, openai_stt, openai_tts  # noqa: F401  (importing registers the types)


def provider_kind(type_name: str) -> Kind | None:
    _load_builtin()
    entry = _REGISTRY.get(type_name)
    return entry[0] if entry else None


def build_provider(name: str, cfg: ProviderConfig, kind: Kind, http: httpx.AsyncClient) -> Any:
    _load_builtin()
    if cfg.type not in _REGISTRY:
        raise ProviderError(f"provider '{name}': unknown type '{cfg.type}'")
    registered_kind, factory = _REGISTRY[cfg.type]
    if registered_kind != kind:
        raise ProviderError(f"provider '{name}' is {cfg.type} ({registered_kind}) but was used as {kind}")
    try:
        return factory(cfg.options(), http)
    except (KeyError, TypeError, ValueError) as e:
        raise ProviderError(f"provider '{name}': invalid or missing option: {e}") from e


def build_provider_set(config: AppConfig, profile: ProfileConfig, http: httpx.AsyncClient) -> ProviderSet:
    return ProviderSet(
        stt=build_provider(profile.stt, config.providers[profile.stt], "stt", http),
        responder=build_provider(profile.responder, config.providers[profile.responder], "responder", http),
        tts=build_provider(profile.tts, config.providers[profile.tts], "tts", http),
    )
