"""Fake providers: run the whole pipeline without external services (tests, demo, reference client)."""

import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import numpy as np

from ..audio import frame_bytes, split_frames
from . import register


@register("fake_stt", "stt")
class FakeStt:
    def __init__(self, options: dict[str, Any], http: httpx.AsyncClient) -> None:
        self.text = str(options.get("text", "hello"))

    async def transcribe(self, wav: bytes, language: str) -> str:
        return self.text


@register("echo_chat", "responder")
class EchoChat:
    def __init__(self, options: dict[str, Any], http: httpx.AsyncClient) -> None:
        self.prefix = str(options.get("prefix", "You said: "))

    async def respond(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        words = (self.prefix + last).split(" ")
        for i, word in enumerate(words):
            yield word if i == len(words) - 1 else word + " "


@register("tone_tts", "tts")
class ToneTts:
    def __init__(self, options: dict[str, Any], http: httpx.AsyncClient) -> None:
        self.sample_rate = int(options.get("sample_rate", 24000))
        self.ms_per_char = int(options.get("ms_per_char", 10))
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive (got {self.sample_rate})")
        if self.ms_per_char < 0:
            raise ValueError(f"ms_per_char cannot be negative (got {self.ms_per_char})")

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        total_ms = max(100, len(text) * self.ms_per_char)
        n = self.sample_rate * total_ms // 1000
        t = np.arange(n) / self.sample_rate
        pcm = (0.2 * np.sin(2 * math.pi * 220.0 * t) * 32767).astype("<i2").tobytes()
        for chunk in split_frames(pcm, frame_bytes(self.sample_rate, 100)):
            yield chunk
