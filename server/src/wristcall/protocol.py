"""Protocol v1 between client (watch, reference client) and server. See docs/protocol.md."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

PROTOCOL_VERSION = 1
INPUT_SAMPLE_RATE = 16_000

CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 4400
CLOSE_UNAUTHORIZED = 4401

# How the user's turn ends: "auto" by silence (VAD), mute or limit; "manual" only by mute or limit.
TurnEnd = Literal["auto", "manual"]


class ErrorCode:
    BAD_MESSAGE = "bad_message"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    UNSUPPORTED_AUDIO = "unsupported_audio"
    UNKNOWN_PROFILE = "unknown_profile"
    AGENT_UNAVAILABLE = "agent_unavailable"
    NOT_STARTED = "not_started"
    STT_FAILED = "stt_failed"
    RESPONDER_FAILED = "responder_failed"
    TTS_FAILED = "tts_failed"
    INTERNAL = "internal"


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AudioFormat(BaseModel):
    codec: str = "pcm16"
    sample_rate: int
    channels: int = 1


class SessionStart(BaseModel):
    type: Literal["session.start"]
    protocol: int
    # Which agent to call: `agent` (slug or id) wins over `profile` (slug, the 0.2.0 name). Neither: the first agent.
    agent: str | None = Field(default=None, max_length=64)
    profile: str | None = Field(default=None, max_length=64)
    audio_in: AudioFormat
    # Absent: the agent's own turn_end when `agent` is named; otherwise "auto" (0.2.x clients send only `profile`).
    turn_end: TurnEnd | None = None

    @field_validator("turn_end", mode="before")
    @classmethod
    def _no_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("turn_end must be 'auto' or 'manual'")
        return value


class Mute(BaseModel):
    type: Literal["mute"]
    muted: bool


class SessionEnd(BaseModel):
    type: Literal["session.end"]


ClientMessage = Annotated[SessionStart | Mute | SessionEnd, Field(discriminator="type")]
_client_adapter: TypeAdapter[SessionStart | Mute | SessionEnd] = TypeAdapter(ClientMessage)


def parse_client_message(text: str) -> SessionStart | Mute | SessionEnd:
    try:
        return _client_adapter.validate_json(text)
    except ValidationError as e:
        first = e.errors()[0]
        raise ProtocolError(ErrorCode.BAD_MESSAGE, f"invalid message: {first['msg']}") from e


def check_session_start(msg: SessionStart) -> None:
    if msg.protocol != PROTOCOL_VERSION:
        raise ProtocolError(ErrorCode.UNSUPPORTED_PROTOCOL, f"protocol {msg.protocol} not supported; use {PROTOCOL_VERSION}")
    audio = msg.audio_in
    if audio.codec != "pcm16" or audio.sample_rate != INPUT_SAMPLE_RATE or audio.channels != 1:
        raise ProtocolError(ErrorCode.UNSUPPORTED_AUDIO, f"input audio must be pcm16 {INPUT_SAMPLE_RATE} Hz mono")


def session_ready(
    session_id: str, agent: dict[str, Any], turn_end: TurnEnd, audio_out: AudioFormat, *, call_id: str | None = None
) -> dict[str, Any]:
    """`agent` is the agent summary (id, slug, display_name, icon, call_type, turn_end).

    `profile` repeats slug and display_name in the 0.2.0 shape, which watch 0.1.0 requires. `call_id` (every call,
    conversation included) is what the client asks `GET /v1/calls/{call_id}` about after hanging up.
    """
    msg = {
        "type": "session.ready",
        "session_id": session_id,
        "profile": {"name": agent["slug"], "display_name": agent["display_name"]},
        "agent": agent,
        "turn_end": turn_end,
        "audio_out": audio_out.model_dump(),
    }
    if call_id is not None:
        msg["call_id"] = call_id
    return msg


def call_captured(call_id: str, reason: Literal["limit"]) -> dict[str, Any]:
    """One-way call: the server stopped recording by itself (time limit) and closes the call (1000)."""
    return {"type": "call.captured", "call_id": call_id, "reason": reason}


def turn_user_end(reason: Literal["vad", "mute", "limit"]) -> dict[str, Any]:
    return {"type": "turn.user_end", "reason": reason}


def transcript(role: Literal["user", "assistant"], text: str) -> dict[str, Any]:
    return {"type": "transcript", "role": role, "text": text}


def agent_start() -> dict[str, Any]:
    return {"type": "turn.agent_start"}


def agent_end() -> dict[str, Any]:
    return {"type": "turn.agent_end"}


def error(code: str, message: str, fatal: bool) -> dict[str, Any]:
    return {"type": "error", "code": code, "message": message, "fatal": fatal}
