"""History export (design decision 16.6): Markdown to read, JSON to keep or import. Times in UTC."""

import json
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from .delivery import iso

EXPORT_VERSION = 1
ROLE_LABEL = {"user": "You", "agent": "Agent"}


class TimeError(ValueError):
    pass


def parse_time(value: str) -> float:
    """Unix seconds, or ISO 8601 (a date, or a date and time; without an offset it is UTC)."""
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds):
            raise TimeError(f"not a finite time: {value!r}")
        return seconds
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TimeError(f"not a time: {value!r} (use Unix seconds or ISO 8601, like 2026-10-09 or 2026-10-09T08:00:00Z)") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _minute(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def markdown_header(now: float) -> str:
    return f"# wristcall history\n\nExported {iso(now)}. Times in UTC, newest first.\n"


def markdown_call(view: dict[str, Any]) -> str:
    agent = view["agent"]["display_name"] or view["agent"]["slug"] or view["agent"]["id"]
    status = view["status"] + (f" ({view['error']})" if view["error"] else "")
    lines = [f"\n## {_minute(view['created_at'])} | {agent} | {view['call_type']} | {status}\n"]
    if view["call_type"] != "conversation" and view["attempts"]:
        http = f", last HTTP {view['last_http_status']}" if view["last_http_status"] is not None else ""
        lines.append(f"Delivery: {view['attempts']} attempt(s){http}.\n")
    for entry in view["entries"]:
        text = entry["text"] if entry["text"] is not None else "(no text)"
        note = f" _({entry['error']})_" if entry["error"] else ""
        lines.append(f"**{ROLE_LABEL.get(entry['role'], entry['role'])}:** {text}{note}\n")
    if not view["entries"]:
        lines.append("_(nothing said)_\n")
    return "\n".join(lines)


async def export_markdown(views: AsyncIterator[dict[str, Any]], now: float) -> AsyncIterator[str]:
    yield markdown_header(now)
    async for view in views:
        yield markdown_call(view)


async def export_json(views: AsyncIterator[dict[str, Any]], now: float) -> AsyncIterator[str]:
    """{"version": 1, "exported_at": ..., "calls": [...]}, written as it goes."""
    yield f'{{"version": {EXPORT_VERSION}, "exported_at": "{iso(now)}", "calls": ['
    first = True
    async for view in views:
        yield ("\n" if first else ",\n") + json.dumps(view, ensure_ascii=False)
        first = False
    yield "\n]}\n"
