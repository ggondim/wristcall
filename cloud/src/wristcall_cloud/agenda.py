"""Validation of what the apps send about their agenda (servers and agents). Pure functions.

Error messages name the field, never its value: a URL may carry credentials the caller should not have sent.
"""

import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

SERVER_KINDS = ("self-hosted", "cloud")
CALL_TYPES = ("conversation", "one-shot", "monologue")
MAX_URL = 2048
MAX_TEXT = 64

_SLUG = re.compile(r"[A-Za-z0-9_-]+")
_ICON = re.compile(r"[a-z0-9.]+")
_SERVER_FIELDS = {"name", "url", "kind", "linked"}
_PATCH_FIELDS = {"name", "linked"}
_AGENT_FIELDS = {"id", "slug", "display_name", "icon", "call_type"}


class AgendaError(Exception):
    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str):
        raise AgendaError(f"{field} must be a string")
    text = value.strip()
    if not 1 <= len(text) <= MAX_TEXT:
        raise AgendaError(f"{field} must be 1 to {MAX_TEXT} characters")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise AgendaError(f"{field} must not contain control characters")
    if pattern is not None and not pattern.fullmatch(text):
        raise AgendaError(f"{field} has characters that are not allowed")
    return text


def _no_extra(body: dict[str, Any], allowed: set[str], what: str) -> None:
    if set(body) - allowed:
        raise AgendaError(f"{what} has fields that are not allowed")


def _linked(value: Any) -> bool:
    if not isinstance(value, bool):
        raise AgendaError("linked must be true or false")
    return value


def normalize_url(value: Any) -> str:
    """http or https, a host, no credentials, query or fragment; scheme and host lowercase, no trailing slash."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL:
        raise AgendaError(f"url must be a string of 1 to {MAX_URL} characters")
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in value):
        raise AgendaError("url must not contain spaces or control characters")
    if "?" in value or "#" in value:
        raise AgendaError("url must not have a query or fragment")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 (raises ValueError on a bad port)
    except ValueError:
        raise AgendaError("url is not valid") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise AgendaError("url must be http or https")
    if not parts.hostname:
        raise AgendaError("url must have a host")
    if "@" in parts.netloc:
        raise AgendaError("url must not have a user or password")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}".rstrip("/")


def parse_new_server(body: dict[str, Any]) -> dict[str, Any]:
    _no_extra(body, _SERVER_FIELDS, "server")
    if "name" not in body or "url" not in body:
        raise AgendaError("name and url are required")
    kind = body.get("kind", "self-hosted")
    if kind not in SERVER_KINDS:
        raise AgendaError("kind must be self-hosted or cloud")
    return {
        "name": _text(body["name"], "name"),
        "url": normalize_url(body["url"]),
        "kind": kind,
        "linked": _linked(body.get("linked", False)),
    }


def parse_server_patch(body: dict[str, Any]) -> dict[str, Any]:
    """Only name and linked change; a different URL or kind is a different server (delete and create)."""
    _no_extra(body, _PATCH_FIELDS, "patch")
    if not body:
        raise AgendaError("nothing to change")
    changes: dict[str, Any] = {}
    if "name" in body:
        changes["name"] = _text(body["name"], "name")
    if "linked" in body:
        changes["linked"] = _linked(body["linked"])
    return changes


def parse_agents(body: dict[str, Any], limit: int) -> list[dict[str, str]]:
    """The full list of agents of a server. Too many agents is code `limit` (checked before the content)."""
    _no_extra(body, {"agents"}, "body")
    agents = body.get("agents")
    if not isinstance(agents, list):
        raise AgendaError("agents must be a list")
    if len(agents) > limit:
        raise AgendaError(f"a server can have at most {limit} agents", code="limit")
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in agents:
        if not isinstance(item, dict):
            raise AgendaError("each agent must be an object")
        _no_extra(item, _AGENT_FIELDS, "agent")
        if set(item) != _AGENT_FIELDS:
            raise AgendaError("each agent needs id, slug, display_name, icon and call_type")
        call_type = item["call_type"]
        if call_type not in CALL_TYPES:
            raise AgendaError("call_type must be conversation, one-shot or monologue")
        agent = {
            "id": _text(item["id"], "id", _SLUG),
            "slug": _text(item["slug"], "slug", _SLUG),
            "display_name": _text(item["display_name"], "display_name"),
            "icon": _text(item["icon"], "icon", _ICON),
            "call_type": call_type,
        }
        if agent["id"] in seen:
            raise AgendaError("agent ids must be unique")
        seen.add(agent["id"])
        parsed.append(agent)
    return parsed
