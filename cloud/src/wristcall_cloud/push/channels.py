"""What a channel sends and how it fails. A channel delivers one message to one registration (APNs, Web Push)."""

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("wristcall_cloud.push")


@dataclass(frozen=True)
class Message:
    event: str  # "call.finished" | "device.approval" | "test"
    title: str  # 1..100
    body: str  # 0..300
    subtitle: str  # the registration's label (forced)
    data: dict[str, Any]  # JSON, up to 1024 bytes serialized
    ttl_s: int  # 0..86400, default 3600
    collapse_id: str | None  # up to 64; APNs apns-collapse-id, Web Push Topic
    tag: str = ""  # the registration's tag (forced): which server the device registered for


class ChannelGone(Exception):
    """The device's channel no longer exists: delete the registration."""


class ChannelUnavailable(Exception):
    """Try later (push service down, throttled, not configured)."""


class Channel(Protocol):
    async def send(self, registration: dict[str, Any], message: Message) -> None:
        """Raises ChannelGone or ChannelUnavailable. Never logs the device token or the endpoint."""
        ...


@dataclass
class FakeChannel:
    """Records what it is given instead of delivering it. `gone` holds channels (token or endpoint) that no longer
    exist; `down` makes every send unavailable."""

    sent: list[tuple[dict[str, Any], Message]] = field(default_factory=list)
    gone: set[str] = field(default_factory=set)
    down: bool = False

    async def send(self, registration: dict[str, Any], message: Message) -> None:
        if self.down:
            raise ChannelUnavailable("fake channel is down")
        if registration["channel"] in self.gone:
            raise ChannelGone("fake channel is gone")
        self.sent.append((registration, message))
        log.info("fake push accepted (platform %s, event %s)", registration["platform"], message.event)
