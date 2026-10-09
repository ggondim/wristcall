"""TTS OpenAI-compatible: POST {base_url}/audio/speech with response_format=pcm (openedai-speech/XTTS, OpenAI)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..audio import MAX_SAMPLE_RATE, MIN_SAMPLE_RATE, ByteAligner
from . import ProviderError, auth_headers, register


@register("openai_tts", "tts")
class OpenAITts:
    def __init__(self, options: dict[str, Any], http: httpx.AsyncClient) -> None:
        self._http = http
        self._url = str(options["base_url"]).rstrip("/") + "/audio/speech"
        self._model = str(options["model"])
        self._voice = str(options.get("voice") or "alloy")
        self._headers = auth_headers(options.get("api_key"))
        self._speed = options.get("speed")
        self.sample_rate = int(options.get("sample_rate", 24000))
        if not MIN_SAMPLE_RATE <= self.sample_rate <= MAX_SAMPLE_RATE:
            raise ValueError(f"sample_rate must be {MIN_SAMPLE_RATE} to {MAX_SAMPLE_RATE} (got {self.sample_rate})")

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        body: dict[str, Any] = {"model": self._model, "voice": self._voice, "input": text, "response_format": "pcm"}
        if self._speed is not None:
            body["speed"] = self._speed
        aligner = ByteAligner()
        try:
            async with self._http.stream("POST", self._url, json=body, headers=self._headers) as r:
                if r.status_code >= 400:
                    detail = (await r.aread())[:200].decode(errors="replace")
                    raise ProviderError(f"TTS returned {r.status_code}: {detail}")
                async for chunk in r.aiter_bytes():
                    out = aligner.push(chunk)
                    if out:
                        yield out
        except httpx.HTTPError as e:
            raise ProviderError(f"TTS unreachable: {e}") from e
