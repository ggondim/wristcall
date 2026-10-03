"""System microphone and speaker via sounddevice (extra `mic`). Enter toggles mute."""

import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from typing import Any


async def mic_source() -> AsyncIterator[bytes | dict[str, Any]]:
    import sounddevice as sd

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | str] = asyncio.Queue()

    def on_audio(indata, frames, time_info, status) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

    def on_keys() -> None:
        for _ in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, "toggle")

    threading.Thread(target=on_keys, daemon=True).start()
    muted = False
    with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=320, callback=on_audio):
        while True:
            item = await queue.get()
            if item == "toggle":
                muted = not muted
                print("[muted]" if muted else "[microphone open]", flush=True)
                yield {"type": "mute", "muted": muted}
            elif not muted:
                yield item


class Speaker:
    def __init__(self, sample_rate: int) -> None:
        import sounddevice as sd

        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16", callback=self._callback)
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status) -> None:
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        outdata[: len(chunk)] = chunk
        outdata[len(chunk) :] = b"\x00" * (n - len(chunk))

    def play(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()
