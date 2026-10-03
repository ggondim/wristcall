"""Protocol v1 between client (watch, reference client) and server. See docs/protocol.md."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1
INPUT_SAMPLE_RATE = 16_000

CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 4400
CLOSE_UNAUTHORIZED = 4401


class ErrorCode:
    BAD_MESSAGE = "bad_message"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    UNSUPPORTED_AUDIO = "unsupported_audio"
    UNKNOWN_PROFILE = "unknown_profile"
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
    profile: str | None = None
    audio_in: AudioFormat


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


def session_ready(session_id: str, profile_name: str, display_name: str, audio_out: AudioFormat) -> dict[str, Any]:
    return {
        "type": "session.ready",
        "session_id": session_id,
        "profile": {"name": profile_name, "display_name": display_name},
        "audio_out": audio_out.model_dump(),
    }


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
