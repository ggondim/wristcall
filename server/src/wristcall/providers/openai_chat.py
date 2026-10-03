"""OpenAI-compatible responder: POST {base_url}/chat/completions with streaming (LiteLLM, OpenAI, Ollama, vLLM)."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from . import ProviderError, auth_headers, register


@register("openai_chat", "responder")
class OpenAIChat:
    def __init__(self, options: dict[str, Any], http: httpx.AsyncClient) -> None:
        self._http = http
        self._url = str(options["base_url"]).rstrip("/") + "/chat/completions"
        self._model = str(options["model"])
        self._headers = auth_headers(options.get("api_key"))
        self._extra = dict(options.get("extra_body") or {})

    async def respond(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        body = {"model": self._model, "messages": messages, "stream": True, **self._extra}
        try:
            async with self._http.stream("POST", self._url, json=body, headers=self._headers) as r:
                if r.status_code >= 400:
                    detail = (await r.aread())[:200].decode(errors="replace")
                    raise ProviderError(f"responder returned {r.status_code}: {detail}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        raise ProviderError(f"responder failed: {chunk['error']}")
                    for choice in chunk.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            yield content
        except httpx.HTTPError as e:
            raise ProviderError(f"responder unreachable: {e}") from e
