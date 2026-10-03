"""Optional warm-up of STT and TTS providers that unload their model when idle."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .audio import SAMPLE_RATE_IN, pcm16_to_wav
from .config import AppConfig, WarmupConfig
from .providers import build_provider, provider_kind

log = logging.getLogger("wristcall.warmup")
_SILENCE_WAV = pcm16_to_wav(b"\x00\x00" * (SAMPLE_RATE_IN // 2), SAMPLE_RATE_IN)


@dataclass
class WarmupTarget:
    name: str
    kind: str
    provider: Any
    config: WarmupConfig


def warmup_targets(config: AppConfig, http: httpx.AsyncClient) -> list[WarmupTarget]:
    targets: list[WarmupTarget] = []
    for name, pcfg in config.providers.items():
        if pcfg.warmup is None:
            continue
        kind = provider_kind(pcfg.type)
        if kind not in ("stt", "tts"):
            log.warning("warm-up ignored for %s: only valid for STT and TTS", name)
            continue
        targets.append(WarmupTarget(name, kind, build_provider(name, pcfg, kind, http), pcfg.warmup))
    return targets


async def warm_one(target: WarmupTarget) -> bool:
    started = time.monotonic()
    try:
        if target.kind == "stt":
            await target.provider.transcribe(_SILENCE_WAV, target.config.language)
        else:
            async for _ in target.provider.synthesize(target.config.text):
                pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("warm-up of %s failed: %s", target.name, e)
        return False
    log.info("warm-up of %s took %.1f s", target.name, time.monotonic() - started)
    return True


async def warm_all(targets: list[WarmupTarget]) -> None:
    await asyncio.gather(*(warm_one(t) for t in targets))


async def run_background(
    targets: list[WarmupTarget], *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> None:
    """Warms the `on_start` targets once, then the `every_s > 0` ones in a loop, until cancelled."""
    await warm_all([t for t in targets if t.config.on_start])
    periodic = [t for t in targets if t.config.every_s > 0]

    async def loop(target: WarmupTarget) -> None:
        while True:
            await sleep(target.config.every_s)
            await warm_one(target)

    if periodic:
        await asyncio.gather(*(loop(t) for t in periodic))
