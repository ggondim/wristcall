"""STT OpenAI-compatible: POST {base_url}/audio/transcriptions (Speaches, OpenAI, Groq)."""

from typing import Any

import httpx

from . import ProviderError, auth_headers, register


def _form_value(v: Any) -> str:
    return str(v).lower() if isinstance(v, bool) else str(v)


@register("openai_stt", "stt")
class OpenAIStt:
    def __init__(self, options: dict[str, Any], http: httpx.AsyncClient) -> None:
        self._http = http
        self._url = str(options["base_url"]).rstrip("/") + "/audio/transcriptions"
        self._model = str(options["model"])
        self._headers = auth_headers(options.get("api_key"))
        self._extra = {k: _form_value(v) for k, v in (options.get("extra_form") or {}).items()}

    async def transcribe(self, wav: bytes, language: str) -> str:
        data = {"model": self._model, "language": language, "response_format": "json", **self._extra}
        try:
            r = await self._http.post(
                self._url, data=data, files={"file": ("turn.wav", wav, "audio/wav")}, headers=self._headers
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"STT unreachable: {e}") from e
        if r.status_code >= 400:
            raise ProviderError(f"STT returned {r.status_code}: {r.text[:200]}")
        try:
            payload = r.json()
        except ValueError as e:
            raise ProviderError(f"STT returned an invalid response: {r.text[:200]}") from e
        if not isinstance(payload, dict):
            raise ProviderError(f"STT returned an invalid response: {r.text[:200]}")
        return str(payload.get("text") or "").strip()
