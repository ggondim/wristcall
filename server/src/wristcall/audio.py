"""PCM16 mono little-endian audio utilities."""

import io
import wave

import numpy as np

SAMPLE_RATE_IN = 16_000
FRAME_MS = 20
SAMPLE_WIDTH = 2
# Sample rates a TTS provider may declare (output to the watch).
MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 48_000


def frame_bytes(sample_rate: int, frame_ms: int = FRAME_MS) -> int:
    return sample_rate * frame_ms // 1000 * SAMPLE_WIDTH


FRAME_BYTES_IN = frame_bytes(SAMPLE_RATE_IN)


def duration_ms(pcm: bytes, sample_rate: int) -> float:
    return len(pcm) / SAMPLE_WIDTH / sample_rate * 1000


def pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != SAMPLE_WIDTH:
            raise ValueError("WAV must be mono PCM16")
        return w.readframes(w.getnframes()), w.getframerate()


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def split_frames(pcm: bytes, size: int) -> list[bytes]:
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]


class FrameAssembler:
    """Joins chunks of arbitrary size and returns only complete frames of `size` bytes."""

    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("size must be positive")
        self._size = size
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        out: list[bytes] = []
        while len(self._buf) >= self._size:
            out.append(bytes(self._buf[: self._size]))
            del self._buf[: self._size]
        return out

    def drain(self) -> bytes:
        """Returns the remainder padded with silence up to a full frame, or b"" if there is no remainder."""
        if not self._buf:
            return b""
        out = bytes(self._buf) + b"\x00" * (self._size - len(self._buf))
        self._buf.clear()
        return out

    def reset(self) -> None:
        self._buf.clear()


class ByteAligner:
    """Ensures each returned chunk has an even number of bytes (whole PCM16 samples)."""

    def __init__(self) -> None:
        self._carry = b""

    def push(self, data: bytes) -> bytes:
        data = self._carry + data
        cut = len(data) - (len(data) % SAMPLE_WIDTH)
        self._carry = data[cut:]
        return data[:cut]
